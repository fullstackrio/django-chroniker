import logging
import os
import sys
import time
import errno
import socket
from functools import partial
from optparse import make_option
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import connection
import django
from django.conf import settings
from django.utils import timezone

from multiprocessing import Process, Queue

import psutil

from chroniker.models import Job, Log, order_by_dependencies
from chroniker import constants as c
from chroniker.utils import pid_exists, TimedProcess

class JobProcess(TimedProcess):
    
    def __init__(self, job, *args, **kwargs):
        super(JobProcess, self).__init__(*args, **kwargs)
        self.job = job

def run_job(job, update_heartbeat=None, stdout_queue=None, stderr_queue=None, **kwargs):
    print "Running Job: %i - '%s' with args: %s" \
        % (job.id, job, job.args)
    # TODO:Fix? Remove multiprocess and just running all jobs serially?
    # Multiprocessing does not play well with Django's PostgreSQL
    # connection, as it seems Django's connection code is not thread-safe.
    # It's a hacky solution, but the short-term fix seems to be to close
    # the connection in this thread, forcing Django to open a new
    # connection unique to this thread.
    # Without this call to connection.close(), we'll get the error
    # "Lost connection to MySQL server during query".
    print 'Closing connection.'
    connection.close()
    print 'Connection closed.'
    job.run(
        update_heartbeat=update_heartbeat,
        check_running=False,
        stdout_queue=stdout_queue,
        stderr_queue=stderr_queue,
    )
    #TODO:mark job as not running if still marked?
    #TODO:normalize job termination and cleanup outside of handle_run()?

def run_cron(jobs=None, update_heartbeat=True, force_run=False):
    try:
        
        # TODO: auto-kill inactive long-running cron processes whose
        # threads have stalled and not exited properly?
        # Check for 0 cpu usage.
        #ps -p <pid> -o %cpu
        
        stdout_map = defaultdict(list) # {prod_id:[]}
        stderr_map = defaultdict(list) # {prod_id:[]}
        stdout_queue = Queue()
        stderr_queue = Queue()
        
        # Check PID file to prevent conflicts with prior executions.
        # TODO: is this still necessary? deprecate? As long as jobs run by
        # JobProcess don't wait for other jobs, multiple instances of cron
        # should be able to run simeltaneously without issue.
        if settings.CHRONIKER_USE_PID:
            pid = str(os.getpid())
            any_running = Job.objects.all_running().count()
            if not any_running:
                # If no jobs are running, then even if the PID file exists,
                # it must be stale, so ignore it.
                pass
            elif os.path.isfile(pid_fn):
                try:
                    old_pid = int(open(pid_fn, 'r').read())
                    if pid_exists(old_pid):
                        print '%s already exists, exiting' % pid_fn
                        sys.exit()
                    else:
                        print ('%s already exists, but contains stale '
                            'PID, continuing') % pid_fn
                except ValueError:
                    pass
                except TypeError:
                    pass
            file(pid_fn, 'w').write(pid)
            clear_pid = True
        
        procs = []
        if force_run:
            q = Job.objects.all()
            if jobs:
                q = q.filter(id__in=jobs)
        else:
            q = Job.objects.due_with_met_dependencies(jobs=jobs)
            
        if settings.CHRONIKER_AUTO_END_STALE_JOBS:
            Job.objects.end_all_stale()
            
        q = sorted(q, cmp=order_by_dependencies)
        for job in q:
            
            # This is necessary, otherwise we get the exception
            # DatabaseError: SSL error: sslv3 alert bad record mac
            # even through we're not using SSL...
            # This is probably caused by the lack of good support for
            # threading/multiprocessing in Django's ORM.
            # We work around this by forcing Django to use separate
            # connections for each process by explicitly closing the
            # current connection.
            connection.close()
            
            # Immediately mark the job as running so the next jobs can
            # update their dependency check.
            job.is_running = True
            job.save()
            
            # Launch job.
            #proc = JobProcess(job, update_heartbeat=update_heartbeat, name=str(job))
            job_func = partial(run_job, job=job, update_heartbeat=update_heartbeat, name=str(job))
            proc = JobProcess(
                job=job,
                max_seconds=job.timeout_seconds,
                target=job_func,
                kwargs=dict(
                    stdout_queue=stdout_queue,
                    stderr_queue=stderr_queue,
                ))
            proc.start()
            procs.append(proc)
        
        print "%d Jobs are due" % len(procs)
        
        # Wait for all job processes to complete.
        while procs:
            
            while not stdout_queue.empty():
                proc_id, proc_stdout = stdout_queue.get()
                stdout_map[proc_id].append(proc_stdout)
                
            while not stderr_queue.empty():
                proc_id, proc_stderr = stderr_queue.get()
                stderr_map[proc_id].append(proc_stderr)
                
            for proc in list(procs):
                if not proc.is_alive():
                    print 'Process %s ended.' % (proc,)
                    procs.remove(proc)
                elif proc.is_expired:
                    print 'Process %s expired.' % (proc,)
                    proc_id = proc.pid
                    proc.terminate()
                    run_end_datetime = timezone.now()
                    procs.remove(proc)
                    
                    connection.close()
                    Job.objects.update()
                    run_start_datetime = Job.objects.get(id=proc.job.id).last_run_start_timestamp
                    proc.job.is_running = False
                    proc.job.force_run = False
                    proc.job.force_stop = False
                    proc.job.save()
                    
                    # Create log record since the job was killed before it had
                    # a chance to do so.
                    Log.objects.create(
                        job=proc.job,
                        run_start_datetime=run_start_datetime,
                        run_end_datetime=run_end_datetime,
                        success=False,
                        on_time=False,
                        hostname=socket.gethostname(),
                        stdout=''.join(stdout_map[proc_id]),
                        stderr=''.join(stderr_map[proc_id]+['Job exceeded timeout\n']),
                    )
                    
            time.sleep(.1)
            
    finally:
        if settings.CHRONIKER_USE_PID and os.path.isfile(pid_fn) \
        and clear_pid:
            os.unlink(pid_fn)
            
class Command(BaseCommand):
    help = 'Runs all jobs that are due.'
    option_list = BaseCommand.option_list + (
        make_option('--update_heartbeat',
            dest='update_heartbeat',
            default=1,
            help='If given, launches a thread to asynchronously update ' + \
                'job heartbeat status.'),
        make_option('--force_run',
            dest='force_run',
            action='store_true',
            default=False,
            help='If given, forces all jobs to run.'),
        make_option('--jobs',
            dest='jobs',
            default='',
            help='A comma-delimited list of job ids to limit executions to.'),
    )
    
    def handle(self, *args, **options):
        pid_fn = settings.CHRONIKER_PID_FN
        clear_pid = False
        
        # Find specific job ids to run, if any.
        jobs = [
            int(_.strip())
            for _ in options.get('jobs', '').strip().split(',')
            if _.strip().isdigit()
        ]
        update_heartbeat = int(options['update_heartbeat'])
        force_run = options['force_run']
        run_cron(jobs, update_heartbeat=update_heartbeat, force_run=force_run)
        