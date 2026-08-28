import process from "node:process";
import { chromium } from "playwright-core";

const hermesUrl = process.env.HERMES_URL ?? "https://hermes.idol.id";
const hermesPassword = process.env.HERMES_PASSWORD?.trim();
const chromeBin = process.env.CHROME_BIN?.trim();

if (!hermesPassword || !chromeBin) {
  throw new Error("required encrypted Hermes runtime inputs are absent");
}

const repair = String.raw`set -euo pipefail
expected_main=cb2199dff026c1b2d3fbd0caa04d6d323370a9e8
expected_remote=a771641cf3da521fda7a2135cfc2e951e0058e63
branch=fix/byte-emitter-refusal-201
repo=
for candidate in "$PWD" "$HOME/x/idol" "$HOME/x/duo" "$HOME/idol" "/Volumes/d 1/idol" "/Volumes/d 1/duo"; do
    if [ ! -d "$candidate/.git" ] && [ ! -f "$candidate/.git" ]; then
        continue
    fi
    origin=$(git -C "$candidate" remote get-url origin 2>/dev/null || true)
    case "$origin" in
        *github.com/clpi/idol|*github.com/clpi/idol.git|*github.com:clpi/idol|*github.com:clpi/idol.git)
            repo=$candidate
            break
            ;;
    esac
done
if [ -z "$repo" ]; then
    echo IDOL_REPAIR_201_REPO_NOT_FOUND
    exit 40
fi

git -C "$repo" fetch origin main "$branch"
actual_main=$(git -C "$repo" rev-parse refs/remotes/origin/main)
actual_remote=$(git -C "$repo" rev-parse "refs/remotes/origin/$branch")
if [ "$actual_main" != "$expected_main" ]; then
    echo "IDOL_REPAIR_201_MAIN_DRIFT=$actual_main"
    exit 41
fi
if [ "$actual_remote" != "$expected_remote" ]; then
    echo "IDOL_REPAIR_201_BRANCH_DRIFT=$actual_remote"
    exit 42
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/idol-byte-refusal-201.XXXXXX")
cleanup() {
    git -C "$repo" worktree remove --force "$work" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT INT TERM

git -C "$repo" worktree add --detach "$work" "$expected_main"
cd "$work"
git switch -C "$branch"

python3 - <<'PY'
from pathlib import Path

path = Path("src/codegen.zig")
text = path.read_text(encoding="utf-8")
old_error = "pub const E = Allocator.Error || std.Io.Writer.Error || error{NoAllocViolation};"
new_error = "pub const E = Allocator.Error || std.Io.Writer.Error || error{ NoAllocViolation, Unsupported };"
old_quote = '''            // has no byte-sequence realization, so a byte-face quote is
            // refused as a commented placeholder — never silently emitted as
            // a text literal. Same total-but-honest route this switch already
            // uses for unrealizable constructs.
            .quoted => |v| if (ast.quotedLiteralIsByteSequence(v.quote))
                self.p("/* byte-sequence face has no realization in this emitter */ lua_val_nil()", .{})
            else {'''
new_quote = '''            // This generic emitter has no lawful extent-bearing byte carrier.
            // Refuse the backend boundary instead of changing bytes into text
            // or a plausible nil value.
            .quoted => |v| if (ast.quotedLiteralIsByteSequence(v.quote))
                return error.Unsupported
            else {'''
if text.count(old_error) != 1:
    raise SystemExit(f"expected one internal emitter error declaration, found {text.count(old_error)}")
if text.count(old_quote) != 1:
    raise SystemExit(f"expected one byte placeholder producer, found {text.count(old_quote)}")
text = text.replace(old_error, new_error).replace(old_quote, new_quote)
path.write_text(text, encoding="utf-8")

Path("gate/byte-emitter.sh").write_text(r'''#!/bin/sh
# The generic C emitter has no lawful extent-bearing bytes carrier yet.
# It must refuse that realization boundary, never synthesize text or nil.
set -eu
root=${BYTE_EMITTER_ROOT:-$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)}
src=$root/src/codegen.zig
fail() {
    printf 'byte-emitter gate: FAIL %s\n' "$1" >&2
    exit 1
}
[ -f "$src" ] || fail "missing emitter source"
if grep -F 'byte-sequence face has no realization in this emitter' "$src" >/dev/null 2>&1; then
    fail "legacy nil placeholder remains"
fi
if grep -F 'quotedLiteralIsByteSequence(v.quote))' "$src" >/dev/null 2>&1 && \
   ! grep -A1 -F 'quotedLiteralIsByteSequence(v.quote))' "$src" | grep -F 'return error.Unsupported' >/dev/null 2>&1; then
    fail "byte-face producer does not refuse the unsupported realization"
fi
grep -F 'error{ NoAllocViolation, Unsupported }' "$src" >/dev/null 2>&1 \
    || fail "internal emitter error path cannot carry refusal"
printf 'byte-emitter gate: PASS\n'
''', encoding="utf-8")
Path(".github/workflows/issue-201-probe.yml").unlink(missing_ok=True)
PY

chmod +x gate/byte-emitter.sh
zig fmt src/codegen.zig
./gate/byte-emitter.sh
zig build

probe=$(mktemp -d "${TMPDIR:-/tmp}/idol-byte-probe.XXXXXX")
trap 'rm -rf "$probe"; cleanup' EXIT INT TERM
cat >"$probe/text.id" <<'ID'
main: i64 = ()
    print("abc")
    0
ID
cat >"$probe/bytes.id" <<'ID'
main: i64 = ()
    print('abc')
    0
ID

./zig-out/bin/idol dump-c "$probe/text.id" >"$probe/text.c" 2>"$probe/text.err"
set +e
./zig-out/bin/idol dump-c "$probe/bytes.id" >"$probe/bytes.c" 2>"$probe/bytes.err"
bytes_status=$?
set -e
if [ "$bytes_status" -eq 0 ]; then
    echo IDOL_REPAIR_201_BYTES_NOT_REFUSED
    exit 43
fi
if grep -F 'lua_val_nil()' "$probe/bytes.c" >/dev/null 2>&1; then
    echo IDOL_REPAIR_201_NIL_SURVIVED
    exit 44
fi

git diff --check
git add src/codegen.zig gate/byte-emitter.sh .github/workflows/issue-201-probe.yml
git -c user.name='Idol architecture agent' -c user.email='chris@pecunies.com' \
    commit -m 'fix: refuse unrealizable byte literals in generic emitter'
git push origin "HEAD:refs/heads/$branch" --force-with-lease="$branch:$expected_remote"
echo IDOL_REPAIR_201_DONE
`;

function encodeCommand(script) {
  const payload = Buffer.from(script, "utf8").toString("base64");
  const runner = [
    "import base64,subprocess,sys",
    `script=base64.b64decode('${payload}').decode('utf-8')`,
    "run=subprocess.run(['bash','-lc',script],text=True)",
    "sys.exit(run.returncode)",
  ].join(";");
  return `python3 -c \"${runner}\"`;
}

async function openTerminal() {
  const browser = await chromium.launch({
    executablePath: chromeBin,
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  try {
    await page.goto(hermesUrl, { waitUntil: "domcontentloaded", timeout: 45_000 });
    await page.waitForTimeout(1_000);
    const password = page.locator('input[type="password"]');
    if (await password.count() > 0 && await password.first().isVisible()) {
      await password.first().fill(hermesPassword);
      const signIn = page.getByRole("button", { name: "Sign in", exact: true });
      if (await signIn.count() > 0 && await signIn.first().isVisible()) await signIn.first().click();
      else await password.first().press("Enter");
      await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 45_000 }).catch(() => {});
      await page.waitForTimeout(1_500);
    }
    if (new URL(page.url()).pathname.startsWith("/login")) throw new Error("Hermes login failed");
    await page.waitForFunction(() => typeof window.api === "function" && typeof window.S === "object", null, { timeout: 30_000 });
    const state = await page.evaluate(async () => {
      if (!window.S?.session?.session_id || !window.S?.session?.workspace) {
        if (typeof window.newSession !== "function") throw new Error("newSession is unavailable");
        await window.newSession(false, { awaitWorkspaceLoad: true, worktree: false });
      }
      const deadline = Date.now() + 30_000;
      while (Date.now() < deadline && (!window.S?.session?.session_id || !window.S?.session?.workspace)) {
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
      return { sessionId: window.S?.session?.session_id ?? null, workspace: Boolean(window.S?.session?.workspace) };
    });
    if (!state.sessionId || !state.workspace) throw new Error("Hermes session lacks a terminal workspace");
    await page.evaluate(async ({ sessionId }) => {
      await window.api("/api/terminal/start", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, rows: 40, cols: 160, restart: false }),
      });
    }, { sessionId: state.sessionId });
    return { browser, context, page, sessionId: state.sessionId };
  } catch (error) {
    await context.close().catch(() => {});
    await browser.close().catch(() => {});
    throw error;
  }
}

async function main() {
  const report = {
    schema: "idol.hermes.byte-refusal-201.v1",
    submitted: false,
    waitedMs: 0,
    semanticOutcomeClaimed: false,
  };
  let hermes;
  try {
    hermes = await openTerminal();
    const command = encodeCommand(repair);
    await hermes.page.evaluate(async ({ sessionId, data }) => {
      await window.api("/api/terminal/input", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, data }),
      });
    }, { sessionId: hermes.sessionId, data: `${command}\r` });
    report.submitted = true;
    const waitMs = 240_000;
    await hermes.page.waitForTimeout(waitMs);
    report.waitedMs = waitMs;
    report.nextEvidence = "verify guarded branch head and diff through GitHub";
  } catch (error) {
    report.error = { name: error?.name, message: String(error?.message ?? error).slice(0, 500) };
    process.exitCode = 1;
  } finally {
    if (hermes) {
      await hermes.page.evaluate(async ({ sessionId }) => {
        await window.api("/api/terminal/close", {
          method: "POST",
          body: JSON.stringify({ session_id: sessionId }),
        });
      }, { sessionId: hermes.sessionId }).catch(() => {});
      await hermes.context.close().catch(() => {});
      await hermes.browser.close().catch(() => {});
    }
    console.log(JSON.stringify(report));
  }
}

await main();
