import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]

class SerializedBootstrap(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.bin=self.home/'fixture-bin'
        self.bin.mkdir()
        self.env={**os.environ,'HOME':str(self.home),'PATH':str(self.bin)+':/usr/bin:/bin'}
        (self.home/'.shared-env').write_text('preserved fixture')
        for name,body in {'node':'echo 22', 'uname':'echo Linux', 'git':'exit 0','curl':'exit 99'}.items():
            self.tool(name,'#!/bin/sh\n'+body+'\n')
        self.tool('npm', '''#!/bin/sh
printf 'start\n' >> "$HOME/install.log"
[ ! -e "$HOME/fail" ] || exit 7
n=0
while [ ! -e "$HOME/release" ]; do
  n=$((n+1)); [ "$n" -lt 150 ] || exit 9
  sleep .02
done
mkdir -p "$HOME/.local/bin"
case "$*" in *claude-code*) name=claude;; *) name=codex;; esac
printf '#!/bin/sh\nexit 0\n' > "$HOME/.local/bin/$name"
chmod +x "$HOME/.local/bin/$name"
''')
    def tool(self,name,body):
        path=self.bin/name;path.write_text(body);path.chmod(0o700)
    def run_bootstrap(self):
        return subprocess.run(['/bin/bash',str(ROOT/'scripts/bootstrap.sh')],env=self.env,
                              capture_output=True,text=True,timeout=8)
    def test_simultaneous_install_refuses_before_shared_npm_write(self):
        first=subprocess.Popen(['/bin/bash',str(ROOT/'scripts/bootstrap.sh')],env=self.env,
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            deadline=time.monotonic()+3
            while not (self.home/'install.log').exists() and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue((self.home/'install.log').exists())
            second=self.run_bootstrap()
            self.assertNotEqual(second.returncode,0)
            self.assertIn('Another bootstrap',second.stderr)
            self.assertEqual((self.home/'install.log').read_text().splitlines(),['start'])
        finally:
            (self.home/'release').touch()
            try: first.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                first.kill();first.communicate()
        self.assertEqual(first.returncode,0)
        self.assertEqual(self.run_bootstrap().returncode,0)
        self.assertEqual((self.home/'install.log').read_text().splitlines(),['start','start'])
        self.assertEqual((self.home/'.shared-env').read_text(),'preserved fixture')
    def test_failed_install_releases_only_owned_lock(self):
        (self.home/'fail').touch();(self.home/'release').touch()
        self.assertNotEqual(self.run_bootstrap().returncode,0)
        self.assertFalse((self.home/'.local/.idol-bootstrap-lock').exists())
        (self.home/'fail').unlink()
        self.assertEqual(self.run_bootstrap().returncode,0)
    def test_minimal_path_needs_no_extra_directory_utilities(self):
        (self.bin/'python3').symlink_to(sys.executable)
        self.tool('claude', '#!/bin/sh\nexit 99\n')
        self.tool('codex', '#!/bin/sh\nexit 99\n')
        self.env['PATH'] = str(self.bin)
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home/'.local/.idol-bootstrap-lock').exists())
        self.assertFalse((self.home/'install.log').exists())
    def test_existing_lock_is_preserved_and_npm_not_run(self):
        lock=self.home/'.local/.idol-bootstrap-lock';lock.mkdir(parents=True)
        (self.home/'release').touch()
        result=self.run_bootstrap()
        self.assertNotEqual(result.returncode,0)
        self.assertTrue(lock.exists())
        self.assertFalse((self.home/'install.log').exists())

if __name__=='__main__': unittest.main()
