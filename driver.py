"""Generic subprocess driver: run another team's bot binary as a child process and relay mechfight/1 to it.

Used for practice only (bot.py hybrid.practice): the child gets the same hello and the same observations we get, one
at a time, and its answer is what we send. Our own brain keeps reading every observation, so it can take over at once.

Safety on 1 CPU / 512 MB / read-only disk:
  - stdin is written by a writer thread, so a hung child that stops reading can never block our main loop;
  - stdout is read by a reader thread into a queue; we wait for the answer whose tick matches, up to a deadline;
  - a late answer to an older tick is dropped, the same as the server would;
  - the child's stderr goes straight to our stderr;
  - every child is killed when we exit (atexit), and close() kills it on the free-for-all switch.
Standard library only."""
import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time

_LIVE = []          # every started driver, so atexit can kill the children


def _kill_all():
    for d in list(_LIVE):
        d.close()


atexit.register(_kill_all)


class SubprocessDriver:
    def __init__(self, cmd, cwd=None, name="driver", env=None):
        self.cmd = list(cmd)
        self.cwd = cwd
        self.name = name
        self.env = env
        self.proc = None
        self.out = queue.Queue()        # parsed JSON objects from the child's stdout, None at EOF
        self.inq = queue.Queue()        # lines for the child's stdin, None to stop the writer
        self.dead = False
        self.why = ""
        self.ready_ms = None
        self.timeouts = 0               # answers that missed the deadline
        self.stale = 0                  # answers that came for an older tick (dropped)

    # ------------------------------------------------------------------ process
    def spawn(self, hello):
        """Start the child and send it the hello. Returns False if it cannot be started."""
        try:
            self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=None)
        except Exception as e:
            self._fail("spawn failed: %s" % e)
            return False
        _LIVE.append(self)
        self.t_spawn = time.perf_counter()
        threading.Thread(target=self._reader, name=self.name + "-out", daemon=True).start()
        threading.Thread(target=self._writer, name=self.name + "-in", daemon=True).start()
        self._send(hello)
        return True

    def wait_ready(self, timeout_s):
        """Wait for the child's `ready`. Returns True when it came in time."""
        if self.dead or self.proc is None:
            return False
        end = self.t_spawn + timeout_s
        while True:
            left = end - time.perf_counter()
            if left <= 0:
                self._fail("no ready within %.1f s" % timeout_s)
                return False
            try:
                m = self.out.get(timeout=left)
            except queue.Empty:
                continue
            if m is None:
                self._fail("exited before ready (code %s)" % self.proc.poll())
                return False
            if isinstance(m, dict) and m.get("type") == "ready":
                self.ready_ms = (time.perf_counter() - self.t_spawn) * 1000
                return True

    def start(self, hello, timeout_s=10.0):
        """spawn + wait_ready in one call."""
        return self.spawn(hello) and self.wait_ready(timeout_s)

    def act(self, obs, timeout_s):
        """Send one observation, return the child's action for that tick, or None (late, crashed, bad line)."""
        if self.dead:
            return None
        if self.proc.poll() is not None:
            self._fail("child exited (code %s)" % self.proc.returncode)
            return None
        tick = obs.get("tick")
        self._send(obs)
        end = time.perf_counter() + timeout_s
        while True:
            left = end - time.perf_counter()
            if left <= 0:
                self.timeouts += 1
                return None
            try:
                m = self.out.get(timeout=left)
            except queue.Empty:
                self.timeouts += 1
                return None
            if m is None:
                self._fail("child closed its stdout (exit code %s)" % self.proc.poll())
                return None
            if not isinstance(m, dict):
                continue
            t = m.get("tick")
            if t == tick:
                return m
            self.stale += 1             # an answer to an older observation (it ran late): the server would drop it

    def close(self):
        if self in _LIVE:
            _LIVE.remove(self)
        self.dead = True
        p = self.proc
        if p is None:
            return
        try:
            self.inq.put_nowait(None)
        except Exception:
            pass
        try:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=1.0)
        except Exception:
            pass

    # ------------------------------------------------------------------ internals
    def _fail(self, why):
        if not self.dead:
            self.why = why
            print("%s: %s" % (self.name, why), file=sys.stderr, flush=True)
        self.close()

    def _send(self, msg):
        try:
            self.inq.put_nowait((json.dumps(msg, separators=(",", ":")) + "\n").encode())
        except Exception as e:
            self._fail("could not encode a message: %s" % e)

    def _writer(self):
        p = self.proc
        while True:
            b = self.inq.get()
            if b is None:
                break
            try:
                p.stdin.write(b)
                p.stdin.flush()
            except Exception:
                break
        try:
            p.stdin.close()
        except Exception:
            pass

    def _reader(self):
        f = self.proc.stdout
        while True:
            try:
                line = f.readline()
            except Exception:
                line = b""
            if not line:
                self.out.put(None)
                return
            try:
                self.out.put(json.loads(line))
            except ValueError:
                print("%s: not JSON on stdout: %r" % (self.name, line[:80]), file=sys.stderr, flush=True)
