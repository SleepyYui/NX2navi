"""Exercise the runtime's 64-bit collectible lookup without game files."""
from pathlib import Path
import os
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / 'runtime/debug_tools.c').read_text(encoding='utf-8')
match = re.search(r'static int skull_index\(uint64_t m, uint64_t key\) \{', source)
assert match, 'individual collectible lookup has not been implemented'
start = match.start()
end = source.index('\n}', match.end()) + 2
harness = r'''
#include <assert.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
static unsigned char manager[0xd00];
static struct { uint64_t key; unsigned char rest[8]; } records[4];
static int rd(uint64_t a, void *v, size_t n) {
    if (!a) return 0;
    memcpy(v, (void *)(uintptr_t)a, n); return 1;
}
static uint64_t rd_ptr(uint64_t a) { uint64_t p; memcpy(&p,(void *)(uintptr_t)a,8);return p; }
''' + source[start:end] + r'''
int main(void) {
    uint64_t m=(uintptr_t)manager, p=(uintptr_t)records;
    uint32_t n=4;
    memcpy(manager+0xc58,&n,4);memcpy(manager+0xc60,&p,8);
    records[0].key=1;records[1].key=0x100000001ull;
    records[2].key=0x8000000000000001ull;records[3].key=UINT64_MAX;
    for (int i=0;i<4;i++) assert(skull_index(m,records[i].key)==i);
    assert(skull_index(m,0)==-1);assert(skull_index(m,2)==-1);
    assert(skull_index(m,0x8000000000000000ull)==-1);
    n=0;memcpy(manager+0xc58,&n,4);assert(skull_index(m,1)==-1);
    n=200001;memcpy(manager+0xc58,&n,4);assert(skull_index(m,1)==-1);
    assert(skull_index(0,1)==-1);
    return 0;
}
'''
match = re.search(r'static int skull_tally\(const int now\[SKULL_COUNT\], int8_t want\[SKULL_COUNT\]\) \{', source)
assert match, 'the token count is not derived from the collection flags'
tally = source[match.start():source.index('\n}', match.end()) + 2]
tally_harness = r'''
#include <assert.h>
#include <stdint.h>
enum { SKULL_COUNT = 100 };
''' + tally + r'''
int main(void) {
    int now[SKULL_COUNT] = {0};
    int8_t want[SKULL_COUNT];
    for (int i = 0; i < SKULL_COUNT; i++) want[i] = -1;
    assert(skull_tally(now, want) == 0);
    for (int i = 0; i < 11; i++) now[i] = 1;
    assert(skull_tally(now, want) == 11);
    // Eighteen changes asked for and not applied by the game yet count already.
    for (int i = 11; i < 29; i++) want[i] = 1;
    assert(skull_tally(now, want) == 29);
    assert(want[11] == 1);
    // Applied ones are forgotten and still count once.
    for (int i = 11; i < 29; i++) now[i] = 1;
    assert(skull_tally(now, want) == 29);
    for (int i = 11; i < 29; i++) assert(want[i] == -1);
    // Taking one back lowers the count before the game has applied it.
    want[0] = 0;
    assert(skull_tally(now, want) == 28);
    assert(want[0] == 0);
    // A flag that cannot be read leaves the count alone, unless a change for it is pending.
    now[50] = -1;
    assert(skull_tally(now, want) == -1);
    want[50] = 1;
    assert(skull_tally(now, want) == 29);
    return 0;
}
'''
match = re.search(r'static int32_t skull_token_delta\(int target, int now\) \{', source)
assert match, 'the token count is not changed by a difference'
start = source.rindex('\nstatic int32_t token_base', 0, match.start()) + 1
delta = source[start:source.index('\n}', match.end()) + 2]
delta_harness = r'''
#include <assert.h>
#include <stdint.h>
''' + delta + r'''
int main(void) {
    // The game adds what is queued; 11 and a target of 29 add 18 once.
    assert(skull_token_delta(29, 11) == 18);
    assert(skull_token_delta(29, 11) == 0);    // not applied yet: nothing more
    assert(skull_token_delta(29, 29) == 0);    // applied
    // A count raised too far comes back down.
    assert(skull_token_delta(29, 243) == -214);
    assert(skull_token_delta(29, 243) == 0);
    assert(skull_token_delta(29, 29) == 0);
    // A second change before the first is applied adds only the rest.
    assert(skull_token_delta(30, 29) == 1);
    assert(skull_token_delta(28, 29) == -2);
    assert(skull_token_delta(28, 28) == 0);
    // A token picked up in the game raises the count and its flag together: nothing to add.
    assert(skull_token_delta(29, 29) == 0);
    assert(skull_token_delta(30, 30) == 0);
    return 0;
}
'''
clang = Path(os.environ.get('NX2NAVI_TEST_CLANG', str(Path(os.environ.get('LOCALAPPDATA', '')) /
             'Programs/NX2navi/toolchain/msys64/ucrt64/bin/clang.exe')))
with tempfile.TemporaryDirectory() as tmp:
    for name, text in (('lookup', harness), ('tally', tally_harness), ('delta', delta_harness)):
        c = Path(tmp) / f'{name}.c'
        exe = Path(tmp) / f'{name}.exe'
        c.write_text(text, encoding='utf-8')
        subprocess.run([str(clang), '-Wall', '-Wextra', '-Werror', str(c), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)
print('Individual collectible lookup and token count checks passed')
