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
clang = Path(os.environ.get('NX2NAVI_TEST_CLANG', str(Path(os.environ.get('LOCALAPPDATA', '')) /
             'Programs/NX2navi/toolchain/msys64/ucrt64/bin/clang.exe')))
with tempfile.TemporaryDirectory() as tmp:
    c = Path(tmp) / 'lookup.c'
    exe = Path(tmp) / 'lookup.exe'
    c.write_text(harness, encoding='utf-8')
    subprocess.run([str(clang), '-Wall', '-Wextra', '-Werror', str(c), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
print('Individual collectible lookup checks passed')
