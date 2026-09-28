#include <assert.h>
#include <limits.h>
#include <stddef.h>
#include <libxml/xmlIO.h>
#include <libxml/xmlerror.h>
#include <libxml/tree.h>
/* Test-only symbol interposition models an oversized output buffer without
 * allocating gigabytes. Actual storage and writes remain in a small real buf. */
size_t xmlBufUse(const xmlBufPtr buf) { (void)buf; return (size_t)INT_MAX + 1; }
static int callbacks;
static int write_cb(void *ctx, const char *data, int len) {
    (void)ctx; (void)data; (void)len; callbacks++; return len;
}
int main(void) {
    xmlOutputBufferPtr out;
    out = xmlOutputBufferCreateIO(write_cb, NULL, NULL, NULL);
    assert(out != NULL);
    assert(xmlOutputBufferWrite(out, 1, "x") == -1);
    assert(out->error == XML_ERR_INTERNAL_ERROR);
    assert(callbacks == 0);
    xmlOutputBufferClose(out);
    out = xmlOutputBufferCreateIO(write_cb, NULL, NULL, NULL);
    assert(out != NULL);
    assert(xmlOutputBufferWriteEscape(out, BAD_CAST "x", NULL) == -1);
    assert(out->error == XML_ERR_INTERNAL_ERROR);
    assert(callbacks == 0);
    xmlOutputBufferClose(out);
    out = xmlOutputBufferCreateIO(write_cb, NULL, NULL, NULL);
    assert(out != NULL);
    assert(xmlOutputBufferFlush(out) == -1);
    assert(out->error == XML_ERR_INTERNAL_ERROR);
    assert(callbacks == 0);
    xmlOutputBufferClose(out);
    return 0;
}
