"""Bounded periodic cleanup in the existing web-service process."""
import logging
import threading
from django.db import close_old_connections, connections
from django.utils import timezone
from .models import PendingSignup

logger = logging.getLogger(__name__)


def cleanup_expired():
    # Batches avoid long transactions. Multiple instances may run safely.
    ids = list(PendingSignup.objects.filter(expires_at__lte=timezone.now())
               .order_by('pk').values_list('pk', flat=True)[:500])
    return PendingSignup.objects.filter(pk__in=ids, expires_at__lte=timezone.now()).delete()[0]


def start_cleanup(interval=3600):
    stop = threading.Event()

    def run():
        while not stop.is_set():
            try:
                close_old_connections()
                deleted = cleanup_expired()
                if deleted:
                    logger.info('Removed %s expired pending signups', deleted)
            except Exception:
                logger.warning('Pending signup cleanup failed; will retry next interval')
            finally:
                connections.close_all()
            if stop.wait(interval):
                break

    threading.Thread(target=run, name='pending-signup-cleanup', daemon=True).start()
    return stop
