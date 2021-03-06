#  Copyright 2008-2015 Nokia Solutions and Networks
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

from robot.errors import (ExecutionFailed, ExecutionFailures, ExecutionPassed,
                          ExitForLoop, ContinueForLoop, DataError,
                          HandlerExecutionFailed)
from robot.result.keyword import Keyword as KeywordResult
from robot.utils import (format_assign_message, frange, get_error_message,
                         get_timestamp, plural_or_not, type_name)
from robot.variables import is_scalar_var, VariableAssigner


class KeywordRunner(object):

    def __init__(self, context, templated=False):
        self._context = context
        self._templated = templated

    def run_keywords(self, keywords):
        errors = []
        for kw in keywords:
            try:
                self.run_keyword(kw)
            except ExecutionPassed as exception:
                exception.set_earlier_failures(errors)
                raise exception
            except ExecutionFailed as exception:
                errors.extend(exception.get_errors())
                if not exception.can_continue(self._context.in_teardown,
                                              self._templated,
                                              self._context.dry_run):
                    break
        if errors:
            raise ExecutionFailures(errors)

    def run_keyword(self, kw, name=None):
        if kw.type == kw.FOR_LOOP_TYPE:
            runner = ForLoopRunner(self._context, self._templated)
        else:
            runner = NormalRunner(self._context)
        return runner.run(kw, name=name)


class NormalRunner(object):

    def __init__(self, context):
        self._context = context

    def run(self, kw, name=None):
        handler = self._context.get_handler(name or kw.name)
        handler.init_keyword(self._context.variables)
        result = KeywordResult(kwname=handler.name or '',
                               libname=handler.libname or '',
                               doc=handler.shortdoc,
                               args=kw.args,
                               assign=self._get_assign(kw.assign),
                               timeout=getattr(handler, 'timeout', ''),
                               type=kw.type,
                               status='NOT_RUN',
                               starttime=get_timestamp())
        self._context.start_keyword(result)
        self._warn_if_deprecated(handler.longname, handler.shortdoc)
        try:
            return_value = self._run(handler, kw)
        except ExecutionFailed as err:
            result.status = self._get_status(err)
            self._end(result, error=err)
            raise
        else:
            if not (self._context.dry_run and handler.type == 'library'):
                result.status = 'PASS'
            self._end(result, return_value)
            return return_value

    def _get_assign(self, assign):
        # TODO: Should use VariableAssigner/Validator here instead
        return tuple(item.rstrip('= ') for item in assign)

    def _warn_if_deprecated(self, name, doc):
        if doc.startswith('*DEPRECATED') and '*' in doc[1:]:
            message = ' ' + doc.split('*', 2)[-1].strip()
            self._context.warn("Keyword '%s' is deprecated.%s" % (name, message))

    def _run(self, handler, kw):
        try:
            # TODO clean this away from self
            self._variable_assigner = VariableAssigner(kw.assign)
            return handler.run(self._context, kw.args[:])
        except ExecutionFailed:
            raise
        except:
            self._report_failure()

    def _get_status(self, error):
        if not error:
            return 'PASS'
        if isinstance(error, ExecutionPassed) and not error.earlier_failures:
            return 'PASS'
        return 'FAIL'

    def _end(self, result, return_value=None, error=None):
        result.endtime = get_timestamp()
        if error and result.type == 'teardown':
            result.message = unicode(error)
        try:
            if not error or error.can_continue(self._context.in_teardown):
                self._set_variables(result, return_value, error)
        finally:
            self._context.end_keyword(result)

    def _set_variables(self, result, return_value, error):
        if error:
            return_value = error.return_value
        try:
            self._variable_assigner.assign(self._context, return_value)
        except DataError as err:
            result.status = 'FAIL'
            msg = unicode(err)
            self._context.output.fail(msg)
            raise ExecutionFailed(msg, syntax=True)

    def _report_failure(self):
        failure = HandlerExecutionFailed()
        if failure.timeout:
            self._context.timeout_occurred = True
        self._context.output.fail(failure.full_message)
        if failure.traceback:
            self._context.output.debug(failure.traceback)
        raise failure


class ForLoopRunner(object):

    def __init__(self, context, templated=False):
        self._context = context
        self._templated = templated

    def run(self, kw, name=None):
        result = KeywordResult(kwname=self._get_name(kw),
                               type=kw.FOR_LOOP_TYPE,
                               starttime=get_timestamp())
        self._context.start_keyword(result)
        error = self._run_with_error_handling(self._validate_and_run, kw)
        result.status = self._get_status(error)
        result.endtime = get_timestamp()
        self._context.end_keyword(result)
        if error:
            raise error

    def _get_name(self, data):
        return '%s %s [ %s ]' % (' | '.join(data.variables),
                                 'IN' if not data.range else 'IN RANGE',
                                 ' | '.join(data.values))

    def _run_with_error_handling(self, runnable, *args):
        try:
            runnable(*args)
        except ExecutionFailed as err:
            return err
        except DataError as err:
            msg = unicode(err)
            self._context.output.fail(msg)
            return ExecutionFailed(msg, syntax=True)
        else:
            return None

    def _validate_and_run(self, data):
        self._validate(data)
        self._run(data)

    def _validate(self, data):
        if not data.variables:
            raise DataError('FOR loop has no loop variables.')
        for var in data.variables:
            if not is_scalar_var(var):
                raise DataError("Invalid FOR loop variable '%s'." % var)
        if not data.values:
            raise DataError('FOR loop has no loop values.')
        if not data.keywords:
            raise DataError('FOR loop contains no keywords.')

    def _run(self, data):
        errors = []
        items, iteration_steps = self._get_items_and_iteration_steps(data)
        for i in iteration_steps:
            values = items[i:i+len(data.variables)]
            exception = self._run_one_round(data, values)
            if exception:
                if isinstance(exception, ExitForLoop):
                    if exception.earlier_failures:
                        errors.extend(exception.earlier_failures.get_errors())
                    break
                if isinstance(exception, ContinueForLoop):
                    if exception.earlier_failures:
                        errors.extend(exception.earlier_failures.get_errors())
                    continue
                if isinstance(exception, ExecutionPassed):
                    exception.set_earlier_failures(errors)
                    raise exception
                errors.extend(exception.get_errors())
                if not exception.can_continue(self._context.in_teardown,
                                              self._templated,
                                              self._context.dry_run):
                    break
        if errors:
            raise ExecutionFailures(errors)

    def _get_items_and_iteration_steps(self, data):
        if self._context.dry_run:
            return data.variables, [0]
        items = self._replace_vars_from_items(self._context.variables, data)
        return items, range(0, len(items), len(data.variables))

    def _replace_vars_from_items(self, variables, data):
        items = variables.replace_list(data.values)
        if data.range:
            items = self._get_range_items(items)
        if len(items) % len(data.variables) == 0:
            return items
        raise DataError('Number of FOR loop values should be multiple of '
                        'variables. Got %d variables but %d value%s.'
                        % (len(data.variables), len(items), plural_or_not(items)))

    def _get_range_items(self, items):
        try:
            items = [self._to_number_with_arithmetics(item) for item in items]
        except:
            raise DataError('Converting argument of FOR IN RANGE failed: %s'
                            % get_error_message())
        if not 1 <= len(items) <= 3:
            raise DataError('FOR IN RANGE expected 1-3 arguments, got %d.'
                            % len(items))
        return frange(*items)

    def _to_number_with_arithmetics(self, item):
        if isinstance(item, (int, long, float)):
            return item
        number = eval(str(item), {})
        if not isinstance(number, (int, long, float)):
            raise TypeError("Expected number, got %s." % type_name(item))
        return number

    def _run_one_round(self, data, values):
        name = ', '.join(format_assign_message(var, item)
                         for var, item in zip(data.variables, values))
        result = KeywordResult(kwname=name,
                               type=data.FOR_ITEM_TYPE,
                               starttime=get_timestamp())
        self._context.start_keyword(result)
        for var, value in zip(data.variables, values):
            self._context.variables[var] = value
        runner = KeywordRunner(self._context, self._templated)
        error = self._run_with_error_handling(runner.run_keywords, data.keywords)
        result.status = self._get_status(error)
        result.endtime = get_timestamp()
        self._context.end_keyword(result)
        return error

    def _get_status(self, error):
        if not error:
            return 'PASS'
        if isinstance(error, ExecutionPassed) and not error.earlier_failures:
            return 'PASS'
        return 'FAIL'
