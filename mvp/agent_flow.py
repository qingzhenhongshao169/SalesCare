"""Bounded, inspectable steps; no autonomous tool registration from tenant prose."""
import time
from contextlib import contextmanager


class FlowExpired(Exception):
    pass


class ReceptionFlow:
    stages = ('route', 'retrieve', 'generate', 'verify', 'deliver')

    def __init__(self, lease_until):
        self.deadline = time.monotonic() + max(0, min(110, lease_until-time.time()))
        self.trace = []
        self.last = -1
        self.generation_calls = 0

    def check(self):
        if time.monotonic() >= self.deadline:
            raise FlowExpired('execution_deadline')

    def remaining(self):
        self.check()
        return max(.1, self.deadline-time.monotonic())

    @contextmanager
    def step(self, name):
        self.check()
        index = self.stages.index(name)
        if index <= self.last:
            raise RuntimeError('Flow steps cannot repeat or move backwards')
        self.last = index
        if name == 'generate':
            if self.generation_calls >= 1:
                raise RuntimeError('Generation budget exceeded')
            self.generation_calls += 1
        step = {'stage': name, 'status': 'running'}
        self.trace.append(step)
        start = time.monotonic()
        try:
            yield
            self.check()
            step['status'] = 'ok'
        except FlowExpired:
            step['status'] = 'expired'
            raise
        except Exception:
            step['status'] = 'failed'
            raise
        finally:
            step['elapsed_ms'] = round((time.monotonic()-start)*1000)

    def diagnostic(self):
        return {'version': 'reception-v2', 'steps': self.trace, 'generation_calls': self.generation_calls,
                'generation_limit': 1}
