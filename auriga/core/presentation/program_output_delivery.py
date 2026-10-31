"""Avoid repeating unchanged log excerpts within a single actor context."""
from copy import deepcopy
from threading import RLock

from ..contracts import digest


class ProgramOutputDelivery:
    def __init__(self):
        self._epoch = None
        self._streams = {}
        self._lock = RLock()

    def invalidate(self):
        with self._lock:
            self._epoch, self._streams = None, {}

    def project(self, result, *, epoch, call_id, detail):
        if detail == 'full' or epoch is None or not call_id or not result.get('job_id'):
            return result
        with self._lock:
            if epoch != self._epoch:
                self._epoch, self._streams = deepcopy(epoch), {}
            result = deepcopy(result)
            for feedback in result.get('visual_feedback', []):
                if feedback.get('delivery', {}).get('status') == 'unavailable':
                    continue
                key = (result['job_id'], 'visual', feedback['call_id'])
                version = feedback['reply_ref']
                previous = self._streams.get(key)
                if previous and previous['version'] == version and previous['call_id'] != call_id:
                    feedback['delivery'] = dict(status='unchanged', scope='current_model_context',
                        previous_tool_call_id=previous['call_id'],
                        read_full=dict(tool='program_status', job_id=result['job_id'], detail='full'))
                else:
                    self._streams[key] = dict(version=version, call_id=call_id)
            for name in ('stdout', 'stderr'):
                text = result.get(name + '_text')
                # Smaller text is cheaper than an unchanged notice and lookup.
                if not isinstance(text, str) or len(text.encode('utf-8')) <= 256:
                    continue
                key = (result['job_id'], name)
                version = digest({field: result.get(name + field) for field in
                    ('_text', '_ref', '_truncated', '_excerpt')})
                previous = self._streams.get(key)
                if previous and previous['version'] == version and previous['call_id'] != call_id:
                    del result[name + '_text']
                    result[name + '_delivery'] = dict(status='unchanged', scope='current_model_context',
                        previous_tool_call_id=previous['call_id'],
                        read_full=dict(tool='program_status', job_id=result['job_id'], detail='full'))
                else:
                    self._streams[key] = dict(version=version, call_id=call_id)
            while len(self._streams) > 256:
                self._streams.pop(next(iter(self._streams)))
            return result
