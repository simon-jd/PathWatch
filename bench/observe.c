#define _GNU_SOURCE
#include <dlfcn.h>
#include <openssl/ssl.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdatomic.h>
#include <string.h>
#include <pthread.h>

// Count real operations only. FIFO syscall time includes syscall overhead;
// it is an upper bound on blocking, not a measurement of pure wait time.
static _Atomic unsigned long writes, bytes, errors, partial, ns, max_ns;
static _Atomic unsigned long handshakes, http1_started;
static ssize_t (*real_write)(int, const void *, size_t);
static int (*real_connect)(SSL *), (*real_handshake)(SSL *);
static int (*real_ssl_write)(SSL *, const void *, int);
static pthread_once_t init_once = PTHREAD_ONCE_INIT;
static _Thread_local int handshake_depth;

static void initialize(void) {
    real_write = dlsym(RTLD_NEXT, "write");
    real_connect = dlsym(RTLD_NEXT, "SSL_connect");
    real_handshake = dlsym(RTLD_NEXT, "SSL_do_handshake");
    real_ssl_write = dlsym(RTLD_NEXT, "SSL_write");
    if (!real_write || !real_connect || !real_handshake || !real_ssl_write)
        _exit(125);
}

static unsigned long now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000000000UL + t.tv_nsec;
}

ssize_t write(int fd, const void *buf, size_t n) {
    pthread_once(&init_once, initialize);
    struct stat st;
    int fifo = fd > 2 && fstat(fd, &st) == 0 && S_ISFIFO(st.st_mode);
    unsigned long start = fifo ? now_ns() : 0;
    ssize_t r = real_write(fd, buf, n);
    if (fifo) {
        unsigned long duration = now_ns() - start;
        atomic_fetch_add(&writes, 1);
        atomic_fetch_add(&ns, duration);
        if (r < 0) atomic_fetch_add(&errors, 1);
        else {
            atomic_fetch_add(&bytes, r);
            if ((size_t)r != n) atomic_fetch_add(&partial, 1);
        }
        unsigned long old = atomic_load(&max_ns);
        while (old < duration && !atomic_compare_exchange_weak(&max_ns, &old, duration)) {}
    }
    return r;
}

static void record_tls(SSL *s) {
    atomic_fetch_add(&handshakes, 1);
    const unsigned char *alpn;
    unsigned len;
    SSL_get0_alpn_selected(s, &alpn, &len);
    const char *sni = SSL_get_servername(s, TLSEXT_NAMETYPE_host_name);
    fprintf(stderr, "BENCH_TLS\t%s\t%s\t%.*s\n", SSL_get_version(s),
            sni ? sni : "(none)", len, alpn);
}

int SSL_connect(SSL *s) {
    pthread_once(&init_once, initialize);
    ++handshake_depth;
    int r = real_connect(s);
    if (--handshake_depth == 0 && r == 1) record_tls(s);
    return r;
}

int SSL_do_handshake(SSL *s) {
    pthread_once(&init_once, initialize);
    ++handshake_depth;
    int r = real_handshake(s);
    if (--handshake_depth == 0 && r == 1) record_tls(s);
    return r;
}

int SSL_write(SSL *s, const void *buf, int num) {
    pthread_once(&init_once, initialize);
    int r = real_ssl_write(s, buf, num);
    // Fixed GET, no request body/pipelining. A partial positive write counts
    // once: wrk advances its pointer, so continuation bytes do not start GET.
    if (r > 0 && num >= 4 && !memcmp(buf, "GET ", 4))
        atomic_fetch_add(&http1_started, 1);
    return r;
}

__attribute__((destructor)) static void finish(void) {
    char *path = getenv("BENCH_OBSERVE");
    if (!path) return;
    FILE *f = fopen(path, "w");
    if (!f) return;
    fprintf(f, "{\"tls_handshakes\":%lu,\"http1_requests_started\":%lu,"
            "\"fifo_write_calls\":%lu,\"fifo_bytes\":%lu,\"fifo_write_errors\":%lu,"
            "\"fifo_partial_writes\":%lu,\"fifo_write_ns\":%lu,\"fifo_max_write_ns\":%lu}\n",
            (unsigned long)handshakes, (unsigned long)http1_started,
            (unsigned long)writes, (unsigned long)bytes, (unsigned long)errors,
            (unsigned long)partial, (unsigned long)ns, (unsigned long)max_ns);
    fclose(f);
}
