import json
import logging
import os
import traceback
from time import time

import django
from celery import Celery, Task
from celery.app.log import TaskFormatter
from celery.signals import after_setup_task_logger
from celery.worker.request import Request
from django.utils import timezone
from redis import Redis
from reNgine.definitions import (FAILED_TASK, INITIATED_TASK, RUNNING_TASK,
                                 SUCCESS_TASK)
from reNgine.settings import (CELERY_RAISE_ON_ERROR, CELERY_TASK_CACHE,
                              CELERY_TASK_CACHE_IGNORE_KWARGS,
                              CELERY_TASK_SKIP_RECORD_ACTIVITY, DEBUG)

cache = Redis.from_url(os.environ['CELERY_BROKER'])
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'reNgine.settings')

# db imports only work if django is loaded, so MUST be placed after django setup
django.setup()
from startScan.models import ScanActivity, ScanHistory


def create_scan_activity(scan_history_id, message, status):
	scan_activity = ScanActivity()
	scan_activity.scan_of = ScanHistory.objects.get(pk=scan_history_id)
	scan_activity.title = message
	scan_activity.time = timezone.now()
	scan_activity.status = status
	scan_activity.save()
	return scan_activity.id


def update_scan_activity(id, status, error=None):
    scan_activity = ScanActivity.objects.filter(id=id)
    if error and len(error) > 300:
        error = error[:288] + '...[trimmed]'
    return scan_activity.update(
			status=status,
			error_message=error,
			time=timezone.now())


class RengineRequest(Request):
    pass


class RengineTask(Task):
    Request = RengineRequest
    def __call__(self, *args, **kwargs):

        # Prepare task
        if self.name == 'reNgine.tasks.skip':
            return
        from celery.utils.log import get_task_logger
        logger = get_task_logger(__name__)
        logger.warning(f'Task {self.name} status is RUNNING')
        task_name = self.name.split('.')[-1]
        args_str = '_'.join([str(arg) for arg in args])
        kwargs_str = '_'.join([f'{k}={v}' for k, v in kwargs.items() if k not in CELERY_TASK_CACHE_IGNORE_KWARGS])
        task_descr = kwargs.pop('description', None) or ' '.join(task_name.split('_')).capitalize()
        if DEBUG > 1:
            task_descr += f' | {args_str} | {kwargs_str}'
        scan_history_id = args[0] if len(args) > 0 else kwargs.get('scan_history_id')

        # Create scan activity only if we have a scan_history_id and 
        # activity_id in the task args
        RECORD_ACTIVITY = (
            scan_history_id is not None
            and
            self.name not in CELERY_TASK_SKIP_RECORD_ACTIVITY
            and
            (
                (len(args) > 1 and isinstance(args[1], int))
                or
                kwargs.get('activity_id', False)
            )
        )

        if RECORD_ACTIVITY:
            activity_id = create_scan_activity(
                scan_history_id=scan_history_id,
                message=task_descr,
                status=INITIATED_TASK)
            # Set activity id as task arg
            if len(args) > 1:
                args = list(args)
                args[1] = activity_id
                args = tuple(args)
            elif 'activity_id' in kwargs:
                kwargs['activity_id'] = activity_id

        # Check for result in cache and return if hit
        if CELERY_TASK_CACHE:
            record_key = f'{self.name}__{args_str}__{kwargs_str}'
            result = cache.get(record_key)
            if result and result != b'null':
                logger.warning(f'Task {self.name} status is SUCCESS (CACHED)')
                if RECORD_ACTIVITY:
                    update_scan_activity(activity_id, SUCCESS_TASK)
                return json.loads(result)

        # Execute task
        result = None
        try:
            if RECORD_ACTIVITY:
                update_scan_activity(activity_id, RUNNING_TASK)
            result = self.run(*args, **kwargs)
            logger.warning(f'Task {self.name} status is SUCCESS')
            if RECORD_ACTIVITY:
                update_scan_activity(activity_id, SUCCESS_TASK)
        except Exception as e:
            logger.error(f'Task {self.name} status is FAILED: {repr(e)}')
            logger.exception(e)
            if RECORD_ACTIVITY:
                error = repr(e)
                if DEBUG > 0:
                    tb = '\n'.join(
                        traceback.format_exception(None, e, e.__traceback__))
                    error += '\n =>' + tb
                update_scan_activity(activity_id, FAILED_TASK, error=error)
            if CELERY_RAISE_ON_ERROR:
                raise e

        # Set task result in cache
        if CELERY_TASK_CACHE and result:
            cache.set(record_key, json.dumps(result))
            cache.expire(record_key, 600) # 10mn cache

        return result

# Celery app
app = Celery('reNgine', task_cls=RengineTask)
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

@after_setup_task_logger.connect
def setup_task_logger(logger, *args, **kwargs):
    for handler in logger.handlers:
        handler.setFormatter(TaskFormatter('%(task_name)s | %(levelname)s | %(message)s'))
