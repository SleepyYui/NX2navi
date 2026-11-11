// The portable ZIP's console entry point. Run the real game exe so the driver can reuse its cache during play.
#include <windows.h>
#include <io.h>
#include <stdio.h>
#include <wchar.h>

static DWORD run_game(int check_only) {
    static wchar_t folder[32768], exe[32768];
    DWORD n = GetModuleFileNameW(NULL, folder, ARRAYSIZE(folder));
    if (!n || n >= ARRAYSIZE(folder)) {
        fputs("Could not locate the extracted game folder.\n", stderr);
        return 1;
    }
    wchar_t *name = wcsrchr(folder, L'\\');
    if (!name) return 1;
    name[1] = 0;
    int len = swprintf(exe, ARRAYSIZE(exe), L"%lsocarina.exe", folder);
    if (len < 0 || (size_t)len >= ARRAYSIZE(exe) || !SetCurrentDirectoryW(folder)) {
        fputs("Could not open the extracted game folder.\n", stderr);
        return 1;
    }
    HANDLE game = CreateFileW(exe, GENERIC_WRITE, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (game == INVALID_HANDLE_VALUE) {
        fprintf(stderr, "Could not open ocarina.exe (Windows error %lu). Extract the ZIP to a writable folder and close the game.\n",
                GetLastError());
        return 1;
    }
    CloseHandle(game);
    HANDLE lock = CreateFileW(L"graphics-prepare.lock", GENERIC_WRITE | DELETE, 0, NULL, OPEN_ALWAYS,
                              FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_DELETE_ON_CLOSE, NULL);
    if (lock == INVALID_HANDLE_VALUE) {
        fprintf(stderr, "Could not lock the graphics tool (Windows error %lu). Close any other copy and try again.\n",
                GetLastError());
        return 1;
    }
    wchar_t check[] = L"ocarina.exe --check-graphics";
    wchar_t prepare[] = L"ocarina.exe --prepare-graphics";
    STARTUPINFOW startup = {.cb = sizeof startup};
    PROCESS_INFORMATION child;
    DWORD result = 1;
    if (!CreateProcessW(exe, check_only ? check : prepare, NULL, NULL, TRUE, 0, NULL, folder, &startup, &child)) {
        fprintf(stderr, "Could not start ocarina.exe (Windows error %lu).\n", GetLastError());
    } else {
        if (WaitForSingleObject(child.hProcess, INFINITE) == WAIT_OBJECT_0)
            GetExitCodeProcess(child.hProcess, &result);
        CloseHandle(child.hThread);
        CloseHandle(child.hProcess);
    }
    CloseHandle(lock);
    if (result > 3) {
        fprintf(stderr, "The game process ended with code 0x%08lx.\n", result);
        if (result == 0xc000001dUL) fputs("This game build may require newer CPU instructions.\n", stderr);
        if (result == 0xc0000135UL) fputs("A required DLL is missing. Check that the entire ZIP was extracted.\n", stderr);
    }
    return result;
}

int wmain(int argc, wchar_t **argv) {
    int check_only = argc == 2 && !wcscmp(argv[1], L"--check-only");
    if (argc != 1 && !check_only) {
        fputs("Usage: Prepare Graphics.exe [--check-only]\n", stderr);
        return 2;
    }
#ifdef CHECK_ONLY
    check_only = 1;
#endif
    DWORD result = run_game(check_only), processes[2];
    if (argc == 1 && _isatty(_fileno(stdin)) && GetConsoleProcessList(processes, ARRAYSIZE(processes)) == 1) {
        fputs("\nPress Enter to close.\n", stdout);
        getchar();
    }
    return (int)result;
}
