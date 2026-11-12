"""Exercise the pass-end lock order with a texture-owning replay worker."""
from pathlib import Path
import argparse,subprocess,os
root=Path(__file__).resolve().parents[1]
ap=argparse.ArgumentParser();ap.add_argument('--cxx',default='clang++');args=ap.parse_args()
s=(root/'runtime/vk_exec.c').read_text(encoding='utf-8');a=s.index('void vk_exec_pass_end(');a=s.index('    uint64_t stamp =',a);b=s.index('\n}',a)
body=s[a:b]
pre=r'''
#include <atomic>
#include <chrono>
#include <mutex>
#include <thread>
#include <stdexcept>
#include <cstdio>
#include <cstdint>
using namespace std::chrono_literals;
struct PlatMutex { std::timed_mutex lock; };
static PlatMutex tfr_lock;
static std::timed_mutex texture;
static std::atomic<bool> waiting{false};
static std::atomic<bool> failed{false};
static thread_local bool second;
static struct { uint64_t frames; } gfx_counters;
struct Target { uintptr_t image, tex; };
struct VkExec { Target color[8],depth; };
struct TfrTarget {};
static TfrTarget tfr_of(const Target *) { return {}; }
static void tfr_note(const TfrTarget *,uint64_t) {}
static int vk_exec_shared() { return 1; }
static void plat_mutex_lock(PlatMutex *m) {
 if(!m->lock.try_lock_for(1s)) throw std::runtime_error("target history lock timed out");
}
static void plat_mutex_unlock(PlatMutex *m) {m->lock.unlock();}
static void vk_texture_gpu_written(uintptr_t) {
 if(second)waiting=true;
 if(!texture.try_lock_for(1s))throw std::runtime_error("texture lock timed out");
 texture.unlock();
}
static void pass_end(VkExec *ex) {
'''
post=r'''
}
int main() {
 texture.lock();
 std::thread worker([]{
  second=true;VkExec ex{};ex.color[0]={1,1};
  try{pass_end(&ex);}catch(const std::exception &e){std::puts(e.what());failed=true;}
 });
 auto deadline=std::chrono::steady_clock::now()+2s;
 while(!waiting && std::chrono::steady_clock::now()<deadline)std::this_thread::yield();
 if(!waiting)failed=true;
 // The first replay worker already owns the texture lock and ends its pass during an inline texture operation.
 VkExec ex{};
 try{pass_end(&ex);}catch(const std::exception &e){std::puts(e.what());failed=true;}
 texture.unlock();worker.join();
 if(failed){std::puts("Pass-end lock inversion reproduced");return 1;}
 std::puts("Pass-end lock ordering passed");return 0;
}
'''
out=root/'build/test/pass-end-lock';out.mkdir(parents=True,exist_ok=True);src=out/'check.cpp';exe=out/'check.exe';src.write_text(pre+body+post,encoding='utf-8')
env=dict(os.environ);env['PATH']=str(Path(args.cxx).resolve().parent)+os.pathsep+env.get('PATH','')
subprocess.run([args.cxx,'-std=gnu++17','-O1',str(src),'-o',str(exe)],env=env,check=True)
subprocess.run([str(exe)],env=env,check=True,timeout=10)
