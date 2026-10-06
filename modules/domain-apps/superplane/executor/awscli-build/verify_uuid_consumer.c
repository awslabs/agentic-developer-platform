/* Ordinary compatibility probe for the actual bundled Python/_uuid consumer.
 * Build tools only; bind-mounted for validation, never copied into runtime. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(void) {
    const char *root = "/opt/awscli-adp/lib/aws-cli";
    const char *library = "/opt/awscli-adp/lib/aws-cli/libuuid.so.1";
    if (setenv("PYTHONHOME", root, 1) ||
        setenv("PYTHONPATH", "/opt/awscli-adp/lib/aws-cli/base_library.zip:"
               "/opt/awscli-adp/lib/aws-cli/python3.14/lib-dynload", 1) ||
        setenv("PYTHONDONTWRITEBYTECODE", "1", 1)) return 1;
    void *python = dlopen("/opt/awscli-adp/lib/aws-cli/libpython3.14.so.1.0",
                          RTLD_NOW | RTLD_GLOBAL);
    if (!python) { fprintf(stderr, "%s\n", dlerror()); return 1; }
    void (*initialize)(int) = dlsym(python, "Py_InitializeEx");
    int (*run)(const char *) = dlsym(python, "PyRun_SimpleString");
    int (*finalize)(void) = dlsym(python, "Py_FinalizeEx");
    if (!initialize || !run || !finalize) return 1;
    initialize(0);
    int result = run("import sys, _uuid\n"
                     "assert sys.version_info[:3] == (3, 14, 7)\n"
                     "assert _uuid.__file__.startswith('/opt/awscli-adp/lib/aws-cli/')\n"
                     "value, status = _uuid.generate_time_safe()\n"
                     "assert len(value) == 16 and value[6] >> 4 == 1\n"
                     "assert status in (0, -1)\n"
                     "print('Bundled Python 3.14.7 _uuid.generate_time_safe: PASS')\n");
    void *uuid = dlopen(library, RTLD_NOW | RTLD_NOLOAD);
    void *symbol = uuid ? dlvsym(uuid, "uuid_generate_time_safe", "UUID_2.20") : NULL;
    Dl_info info;
    if (!symbol || !dladdr(symbol, &info) || strcmp(info.dli_fname, library)) result = 1;
    else puts("Bundled libuuid.so.1 UUID_2.20 loader binding: PASS");
    if (finalize()) result = 1;
    return result ? 1 : 0;
}
