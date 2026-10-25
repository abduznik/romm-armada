// SPDX-License-Identifier: MIT
/*
 * savebridge: a command line front end to argosy-sigil for the RomM Armada Decky plugin.
 *
 * sigil never touches the file system itself. Its caller lists the save root, opens members on
 * request and writes or removes files when a unit is restored. This program is that caller, so the
 * plugin can stay free of any C binding (and of the Python version Decky happens to bundle): it
 * runs this binary, passes paths, and reads one JSON document from stdout. Blobs (the unit sigil
 * builds, the state it asks the caller to keep) travel as files.
 *
 * Exit status: 0 done, 1 sigil refused (the JSON has "code" and "error"), 2 bad command line,
 * 3 internal error.
 */
#include "sigil.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define HELPER_VERSION "0.1.0"

#ifndef SIGIL_PIN
#define SIGIL_PIN "unknown"
#endif

#if defined(__aarch64__)
#define HELPER_ARCH "aarch64"
#elif defined(__x86_64__)
#define HELPER_ARCH "x86_64"
#else
#define HELPER_ARCH "unknown"
#endif

#define LIST_DEPTH 12 /* how deep below a layout's subfolder the listing goes (as sigil's bindings do) */
#define BASE_SEARCH_DEPTH 5

/* ---- small helpers ------------------------------------------------------------------------ */

static void die(int status, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "savebridge: ");
    vfprintf(stderr, fmt, ap);
    fprintf(stderr, "\n");
    va_end(ap);
    exit(status);
}

static void *xmalloc(size_t n) {
    void *p = malloc(n ? n : 1);
    if (!p) die(3, "out of memory");
    return p;
}

static void *xrealloc(void *p, size_t n) {
    void *q = realloc(p, n ? n : 1);
    if (!q) die(3, "out of memory");
    return q;
}

static char *xstrdup(const char *s) {
    size_t n = strlen(s);
    char *p = (char *)xmalloc(n + 1);
    memcpy(p, s, n + 1);
    return p;
}

static char *xprintf(const char *fmt, ...) {
    va_list ap, cp;
    va_start(ap, fmt);
    va_copy(cp, ap);
    int n = vsnprintf(NULL, 0, fmt, ap);
    va_end(ap);
    if (n < 0) die(3, "format error");
    char *p = (char *)xmalloc((size_t)n + 1);
    vsnprintf(p, (size_t)n + 1, fmt, cp);
    va_end(cp);
    return p;
}

typedef struct {
    char **items;
    size_t count, cap;
} strvec;

static void sv_push(strvec *v, char *owned) {
    if (v->count == v->cap) {
        v->cap = v->cap ? v->cap * 2 : 16;
        v->items = (char **)xrealloc(v->items, v->cap * sizeof(char *));
    }
    v->items[v->count++] = owned;
}

static int cmp_str(const void *a, const void *b) { return strcmp(*(char *const *)a, *(char *const *)b); }

static void sv_free(strvec *v) {
    for (size_t i = 0; i < v->count; i++) free(v->items[i]);
    free(v->items);
    memset(v, 0, sizeof(*v));
}

/* A relative path that stays inside its root: no leading '/', no ".." segment, no empty path. */
static bool rel_ok(const char *rel) {
    if (!rel || !*rel || rel[0] == '/') return false;
    const char *s = rel;
    while (*s) {
        const char *e = strchr(s, '/');
        size_t n = e ? (size_t)(e - s) : strlen(s);
        if (n == 2 && s[0] == '.' && s[1] == '.') return false;
        if (!e) break;
        s = e + 1;
    }
    return true;
}

static char *join_path(const char *root, const char *rel) {
    size_t rl = strlen(root);
    while (rl > 1 && root[rl - 1] == '/') rl--;
    return xprintf("%.*s/%s", (int)rl, root, rel);
}

static bool read_file(const char *path, uint8_t **buf, size_t *len) {
    FILE *f = fopen(path, "rb");
    if (!f) return false;
    size_t cap = 1 << 16, n = 0;
    uint8_t *b = (uint8_t *)xmalloc(cap);
    for (;;) {
        if (n == cap) {
            cap *= 2;
            b = (uint8_t *)xrealloc(b, cap);
        }
        size_t got = fread(b + n, 1, cap - n, f);
        n += got;
        if (got == 0) break;
    }
    bool ok = !ferror(f);
    fclose(f);
    if (!ok) {
        free(b);
        return false;
    }
    *buf = b;
    *len = n;
    return true;
}

/* Writes `path` through a temporary file in the same folder, so a card is never left half written. */
static int write_atomic(const char *path, const uint8_t *data, size_t len) {
    char *tmp = xprintf("%s.sbtmp.%ld", path, (long)getpid());
    int fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0666);
    if (fd < 0) {
        free(tmp);
        return -1;
    }
    struct stat old;
    if (stat(path, &old) == 0) {
        if (S_ISDIR(old.st_mode)) {
            close(fd);
            unlink(tmp);
            free(tmp);
            errno = EISDIR;
            return -1;
        }
        (void)fchmod(fd, old.st_mode & 07777);
    }
    size_t off = 0;
    int rc = 0;
    while (off < len) {
        ssize_t w = write(fd, data + off, len - off);
        if (w < 0) {
            if (errno == EINTR) continue;
            rc = -1;
            break;
        }
        off += (size_t)w;
    }
    if (rc == 0 && fsync(fd) != 0) rc = -1;
    if (close(fd) != 0) rc = -1;
    if (rc == 0 && rename(tmp, path) != 0) rc = -1;
    if (rc != 0) {
        int e = errno;
        unlink(tmp);
        errno = e;
    }
    free(tmp);
    return rc;
}

static int mkdir_p(const char *dir) {
    char *p = xstrdup(dir);
    for (char *s = p + 1; *s; s++) {
        if (*s != '/') continue;
        *s = 0;
        if (mkdir(p, 0777) != 0 && errno != EEXIST) {
            free(p);
            return -1;
        }
        *s = '/';
    }
    int rc = mkdir(p, 0777);
    if (rc != 0 && errno == EEXIST) rc = 0;
    free(p);
    return rc;
}

/* ---- JSON out ----------------------------------------------------------------------------- */

typedef struct {
    FILE *out;
    int depth;
    bool comma[32];
    bool after_key;
} jw_t;

static jw_t W = { 0 };

static void jw_value_prefix(jw_t *w) {
    if (w->after_key) {
        w->after_key = false;
        return;
    }
    if (w->comma[w->depth]) fputc(',', w->out);
    w->comma[w->depth] = true;
}

static void jw_string(jw_t *w, const char *s) {
    FILE *f = w->out;
    fputc('"', f);
    const unsigned char *p = (const unsigned char *)s;
    while (*p) {
        unsigned char c = *p;
        if (c == '"' || c == '\\') {
            fputc('\\', f);
            fputc(c, f);
            p++;
        } else if (c == '\n') {
            fputs("\\n", f);
            p++;
        } else if (c == '\r') {
            fputs("\\r", f);
            p++;
        } else if (c == '\t') {
            fputs("\\t", f);
            p++;
        } else if (c < 0x20) {
            fprintf(f, "\\u%04x", c);
            p++;
        } else if (c < 0x80) {
            fputc(c, f);
            p++;
        } else {
            /* UTF-8: copy a whole valid sequence, else one replacement character per bad byte */
            int need = c >= 0xF0 && c < 0xF5 ? 3 : c >= 0xE0 ? 2 : c >= 0xC2 ? 1 : -1;
            bool ok = need > 0 && c < 0xF5;
            for (int i = 1; ok && i <= need; i++) ok = (p[i] & 0xC0) == 0x80;
            if (ok) {
                fwrite(p, 1, (size_t)need + 1, f);
                p += need + 1;
            } else {
                fputs("\\ufffd", f);
                p++;
            }
        }
    }
    fputc('"', f);
}

static void jw_str(jw_t *w, const char *s) {
    jw_value_prefix(w);
    jw_string(w, s ? s : "");
}

static void jw_key(jw_t *w, const char *k) {
    jw_value_prefix(w);
    jw_string(w, k);
    fputc(':', w->out);
    w->after_key = true;
}

static void jw_int(jw_t *w, long long v) {
    jw_value_prefix(w);
    fprintf(w->out, "%lld", v);
}

static void jw_bool(jw_t *w, bool v) {
    jw_value_prefix(w);
    fputs(v ? "true" : "false", w->out);
}

static void jw_open(jw_t *w, char brace) {
    jw_value_prefix(w);
    fputc(brace, w->out);
    if (w->depth + 1 >= (int)(sizeof(w->comma) / sizeof(w->comma[0]))) die(3, "json nesting too deep");
    w->depth++;
    w->comma[w->depth] = false;
}

static void jw_close(jw_t *w, char brace) {
    fputc(brace, w->out);
    w->depth--;
}

#define OBJ() jw_open(&W, '{')
#define END_OBJ() jw_close(&W, '}')
#define ARR() jw_open(&W, '[')
#define END_ARR() jw_close(&W, ']')
#define KEY(k) jw_key(&W, (k))
#define KV_STR(k, v) (jw_key(&W, (k)), jw_str(&W, (v)))
#define KV_INT(k, v) (jw_key(&W, (k)), jw_int(&W, (long long)(v)))
#define KV_BOOL(k, v) (jw_key(&W, (k)), jw_bool(&W, (v)))

static void emit_begin(bool ok) {
    W.out = stdout;
    OBJ();
    KV_BOOL("ok", ok);
}

static void emit_end(void) {
    END_OBJ();
    fputc('\n', W.out);
    fflush(W.out);
}

/* ---- names -------------------------------------------------------------------------------- */

static const char *err_name(int rc) {
    switch (rc) {
    case SIGIL_ERR_INVALID_ARG:        return "invalid_arg";
    case SIGIL_ERR_IO:                 return "io";
    case SIGIL_ERR_UNKNOWN_PLATFORM:   return "unknown_platform";
    case SIGIL_ERR_UNSUPPORTED_FORMAT: return "unsupported_format";
    case SIGIL_ERR_NOT_FOUND:          return "not_found";
    case SIGIL_ERR_NEEDS_KEY:          return "needs_key";
    case SIGIL_ERR_CRYPTO:             return "crypto";
    case SIGIL_ERR_OOM:                return "oom";
    case SIGIL_ERR_CONFLICT:           return "conflict";
    case SIGIL_ERR_EXISTS:             return "exists";
    case SIGIL_ERR_NO_SPACE:           return "no_space";
    case SIGIL_ERR_UNCOLLECTED:        return "uncollected";
    case SIGIL_ERR_DAMAGED:            return "damaged";
    case SIGIL_ERR_REGION:             return "region";
    case SIGIL_ERR_NO_TARGET:          return "no_target";
    case SIGIL_ERR_AMBIGUOUS:          return "ambiguous";
    case SIGIL_ERR_KEYS_INCOMPATIBLE:  return "keys_incompatible";
    default:                           return "error";
    }
}

static const char *shape_name(int s) {
    switch (s) {
    case SIGIL_SAVE_SHAPE_SINGLE: return "single";
    case SIGIL_SAVE_SHAPE_MULTI:  return "multi";
    case SIGIL_SAVE_SHAPE_FOLDER: return "folder";
    default:                      return "none";
    }
}

static const char *role_name(int r) {
    switch (r) {
    case SIGIL_SAVE_ROLE_PRIMARY: return "primary";
    case SIGIL_SAVE_ROLE_RTC:     return "rtc";
    default:                      return "sidecar";
    }
}

static const char *area_name(int a) {
    switch (a) {
    case SIGIL_SAVE_AREA_ACCOUNT: return "account";
    case SIGIL_SAVE_AREA_DEVICE:  return "device";
    default:                      return "none";
    }
}

static const char *usage_name(sigil_usage u) {
    switch (u) {
    case SIGIL_USAGE_FOLDER_EXACT:  return "folder-exact";
    case SIGIL_USAGE_FOLDER_PREFIX: return "folder-prefix";
    case SIGIL_USAGE_FILE_EXACT:    return "file-exact";
    case SIGIL_USAGE_FILE_PREFIX:   return "file-prefix";
    case SIGIL_USAGE_FOLDER_SPLIT:  return "folder-split";
    default:                        return "unknown";
    }
}

/* A refusal: the JSON the plugin reads, and the exit status for "sigil said no". */
static int fail(int rc, const sigil_sync_result *r) {
    emit_begin(false);
    KV_INT("code", rc);
    KV_STR("error", err_name(rc));
    KV_STR("message", sigil_strerror(rc));
    if (r) {
        if (r->problem[0]) KV_STR("problem", r->problem);
        if (r->blocks_short) KV_INT("blocks_short", r->blocks_short);
        if (r->conflict) KV_BOOL("conflict", true);
        if (r->profile_count) {
            KEY("profiles");
            ARR();
            for (size_t i = 0; i < r->profile_count; i++) {
                OBJ();
                KV_STR("id", r->profiles[i].id);
                KV_STR("name", r->profiles[i].name);
                END_OBJ();
            }
            END_ARR();
        }
    }
    emit_end();
    return 1;
}

static int fail_msg(int status, const char *error, const char *fmt, ...) {
    char msg[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
    emit_begin(false);
    KV_INT("code", 0);
    KV_STR("error", error);
    KV_STR("message", msg);
    emit_end();
    return status;
}

/* ---- command line ------------------------------------------------------------------------- */

typedef struct {
    const char *cmd;
    const char *rom, *platform, *layout, *root, *content, *profile, *path, *mode, *prod_keys;
    const char *state_in, *state_out, *unit_in, *unit_out, *holding_out;
    const char *title_id, *save_id, *raw_serial, *n64_header, *n64_md5, *n64_md5_n64;
    uint32_t features;
    bool overwrite_local, repair, hash, no_filename_fallback;
    strvec option_keys, option_values; /* --option KEY=VALUE, split */
    strvec game_ids;
} args_t;

static bool take(const char *a, const char *name, int argc, char **argv, int *i, const char **out) {
    size_t n = strlen(name);
    if (strncmp(a, name, n) != 0) return false;
    if (a[n] == '=') {
        *out = a + n + 1;
        return true;
    }
    if (a[n] != 0) return false;
    if (*i + 1 >= argc) die(2, "%s needs a value", name);
    *out = argv[++(*i)];
    return true;
}

static void parse_args(int argc, char **argv, args_t *a) {
    if (argc < 2) die(2, "usage: savebridge <version|identify|layouts|locate|collect|restore|card-list> [options]");
    a->cmd = argv[1];
    for (int i = 2; i < argc; i++) {
        const char *s = argv[i], *v = NULL;
        if (take(s, "--rom", argc, argv, &i, &a->rom) || take(s, "--platform", argc, argv, &i, &a->platform) ||
            take(s, "--layout", argc, argv, &i, &a->layout) || take(s, "--root", argc, argv, &i, &a->root) ||
            take(s, "--content", argc, argv, &i, &a->content) || take(s, "--profile", argc, argv, &i, &a->profile) ||
            take(s, "--path", argc, argv, &i, &a->path) || take(s, "--mode", argc, argv, &i, &a->mode) ||
            take(s, "--prod-keys", argc, argv, &i, &a->prod_keys) ||
            take(s, "--state-in", argc, argv, &i, &a->state_in) || take(s, "--state-out", argc, argv, &i, &a->state_out) ||
            take(s, "--unit", argc, argv, &i, &a->unit_in) || take(s, "--out", argc, argv, &i, &a->unit_out) ||
            take(s, "--holding-out", argc, argv, &i, &a->holding_out) ||
            take(s, "--title-id", argc, argv, &i, &a->title_id) || take(s, "--save-id", argc, argv, &i, &a->save_id) ||
            take(s, "--raw-serial", argc, argv, &i, &a->raw_serial) ||
            take(s, "--n64-header", argc, argv, &i, &a->n64_header) || take(s, "--n64-md5", argc, argv, &i, &a->n64_md5) ||
            take(s, "--n64-md5-n64", argc, argv, &i, &a->n64_md5_n64)) {
            continue;
        }
        if (take(s, "--features", argc, argv, &i, &v)) {
            a->features = (uint32_t)strtoul(v, NULL, 10);
        } else if (take(s, "--game-id", argc, argv, &i, &v)) {
            sv_push(&a->game_ids, xstrdup(v));
        } else if (take(s, "--option", argc, argv, &i, &v)) {
            const char *eq = strchr(v, '=');
            if (!eq || eq == v) die(2, "--option wants KEY=VALUE, got '%s'", v);
            sv_push(&a->option_keys, xprintf("%.*s", (int)(eq - v), v));
            sv_push(&a->option_values, xstrdup(eq + 1));
        } else if (strcmp(s, "--overwrite-local") == 0) {
            a->overwrite_local = true;
        } else if (strcmp(s, "--repair") == 0) {
            a->repair = true;
        } else if (strcmp(s, "--hash") == 0) {
            a->hash = true;
        } else if (strcmp(s, "--no-filename-fallback") == 0) {
            a->no_filename_fallback = true;
        } else {
            die(2, "unknown option '%s'", s);
        }
    }
}

/* ---- identity of the game ----------------------------------------------------------------- */

typedef struct {
    sigil_result result;
    bool have;       /* result holds a game; false for platforms without a title id */
    char slug[32];   /* platform slug, "" when unknown */
} game_t;

static int identify(const args_t *a, game_t *g, int *rc_out) {
    memset(g, 0, sizeof(*g));
    g->result.struct_version = SIGIL_RESULT_V4;
    if (a->rom) {
        sigil_platform hint = SIGIL_PLATFORM_AUTO;
        if (a->platform && strcmp(a->platform, "auto") != 0) {
            hint = sigil_platform_from_slug(a->platform);
            if (hint == SIGIL_PLATFORM_AUTO) {
                *rc_out = SIGIL_ERR_UNKNOWN_PLATFORM;
                return -1;
            }
        }
        sigil_support sup = { .struct_version = SIGIL_SUPPORT_V1 };
        sup.switch_prod_keys_path = a->prod_keys;
        sigil_options opts = { .struct_version = SIGIL_OPTIONS_V1,
                               .support = a->prod_keys ? &sup : NULL,
                               .flags = a->no_filename_fallback ? 0u : SIGIL_FLAG_FILENAME_FALLBACK };
        int rc = sigil_extract_from_path(a->rom, hint, &opts, &g->result);
        if (rc != SIGIL_OK) {
            *rc_out = rc;
            return -1;
        }
        g->have = true;
        snprintf(g->slug, sizeof(g->slug), "%s", sigil_platform_to_slug(g->result.platform));
        return 0;
    }
    /* a game the caller stored earlier (docs/quickstart-guides/python.md, "persisted") */
    if (a->platform && strcmp(a->platform, "auto") != 0) {
        g->result.platform = sigil_platform_from_slug(a->platform);
        snprintf(g->slug, sizeof(g->slug), "%s", a->platform);
    }
    if (a->title_id) snprintf(g->result.title_id, sizeof(g->result.title_id), "%s", a->title_id);
    if (a->save_id) snprintf(g->result.save_id, sizeof(g->result.save_id), "%s", a->save_id);
    if (a->raw_serial) snprintf(g->result.raw_serial, sizeof(g->result.raw_serial), "%s", a->raw_serial);
    if (a->n64_header) snprintf(g->result.n64_header, sizeof(g->result.n64_header), "%s", a->n64_header);
    if (a->n64_md5) snprintf(g->result.n64_md5, sizeof(g->result.n64_md5), "%s", a->n64_md5);
    if (a->n64_md5_n64) snprintf(g->result.n64_md5_n64, sizeof(g->result.n64_md5_n64), "%s", a->n64_md5_n64);
    g->result.features = a->features;
    g->have = a->title_id || a->save_id;
    return 0;
}

/* ---- listing the save root ---------------------------------------------------------------- */

static bool is_file(const char *path) {
    struct stat st;
    return stat(path, &st) == 0 && S_ISREG(st.st_mode);
}

static bool is_dir(const char *path) {
    struct stat st;
    return stat(path, &st) == 0 && S_ISDIR(st.st_mode);
}

/* The names in `dir`, sorted, so two runs (and two architectures) list a root the same way. */
static void dir_names(const char *dir, strvec *names) {
    DIR *d = opendir(dir);
    if (!d) return;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) continue;
        sv_push(names, xstrdup(e->d_name));
    }
    closedir(d);
    qsort(names->items, names->count, sizeof(char *), cmp_str);
}

static void list_recursive(const char *dir, const char *rel, int depth, strvec *out) {
    if (depth == 0 || !is_dir(dir)) return;
    strvec names = { 0 };
    dir_names(dir, &names);
    for (size_t i = 0; i < names.count; i++) {
        char *abs = join_path(dir, names.items[i]);
        char *r = xprintf("%s/%s", rel, names.items[i]);
        if (is_file(abs)) sv_push(out, r);
        else {
            if (is_dir(abs)) list_recursive(abs, r, depth - 1, out);
            free(r);
        }
        free(abs);
    }
    sv_free(&names);
}

static void bases_walk(const char *dir, const char *rel, const char *top, int depth, strvec *found) {
    if (depth == 0) return;
    strvec names = { 0 };
    dir_names(dir, &names);
    for (size_t i = 0; i < names.count; i++) {
        char *abs = join_path(dir, names.items[i]);
        struct stat st;
        if (lstat(abs, &st) == 0 && S_ISDIR(st.st_mode)) {
            if (strcmp(names.items[i], top) == 0) {
                if (rel[0]) sv_push(found, xstrdup(rel));
            } else {
                char *r = rel[0] ? xprintf("%s/%s", rel, names.items[i]) : xstrdup(names.items[i]);
                bases_walk(abs, r, top, depth - 1, found);
                free(r);
            }
        }
        free(abs);
    }
    sv_free(&names);
}

/* Root-relative paths of the files directly in `root` plus those under the layout's subfolders;
 * on a layout whose emulator base sits below `root`, the subfolders under each such base. */
static void list_save_root(const char *root, const char *layout, strvec *out) {
    if (is_dir(root)) {
        strvec names = { 0 };
        dir_names(root, &names);
        for (size_t i = 0; i < names.count; i++) {
            char *abs = join_path(root, names.items[i]);
            if (is_file(abs)) sv_push(out, xstrdup(names.items[i]));
            free(abs);
        }
        sv_free(&names);
    }
    const char *subdirs[64];
    size_t n = sigil_save_layout_subdirs(layout, subdirs, 64);
    const char *top = sigil_save_layout_top(layout);
    strvec bases = { 0 };
    bool below = false;
    if (top) {
        char *t = join_path(root, top);
        below = !is_dir(t);
        free(t);
    }
    if (below) bases_walk(root, "", top, BASE_SEARCH_DEPTH, &bases);
    if (bases.count == 0) sv_push(&bases, xstrdup(""));
    for (size_t b = 0; b < bases.count; b++) {
        for (size_t i = 0; i < n; i++) {
            char *rel = bases.items[b][0] ? xprintf("%s/%s", bases.items[b], subdirs[i]) : xstrdup(subdirs[i]);
            char *abs = join_path(root, rel);
            list_recursive(abs, rel, LIST_DEPTH, out);
            free(abs);
            free(rel);
        }
    }
    sv_free(&bases);
}

/* ---- callbacks sigil calls into ----------------------------------------------------------- */

typedef struct {
    const char *root;
} fs_ctx;

static sigil_io *cb_open(void *ctx, const char *rel) {
    if (!rel_ok(rel)) return NULL;
    char *p = join_path(((fs_ctx *)ctx)->root, rel);
    sigil_io *io = sigil_io_open_file(p);
    free(p);
    return io;
}

/* Where a write to `path` really lands: through a symlink (a card on the SD card, say), not over it. */
static char *resolve_target(const char *path) {
    struct stat st;
    if (lstat(path, &st) == 0 && S_ISLNK(st.st_mode)) {
        char *real = realpath(path, NULL);
        if (real) {
            char *dup = xstrdup(real);
            free(real);
            return dup;
        }
    }
    return xstrdup(path);
}

static int cb_write(void *ctx, const char *rel, const uint8_t *data, size_t len) {
    if (!rel_ok(rel)) return -1;
    char *abs = join_path(((fs_ctx *)ctx)->root, rel);
    char *target = resolve_target(abs);
    free(abs);
    char *slash = strrchr(target, '/');
    int rc = 0;
    if (slash && slash != target) {
        *slash = 0;
        rc = mkdir_p(target);
        *slash = '/';
    }
    if (rc == 0) rc = write_atomic(target, data, len);
    free(target);
    return rc == 0 ? 0 : -1;
}

static int cb_remove(void *ctx, const char *rel) {
    size_t n = strlen(rel);
    bool dir = n > 0 && rel[n - 1] == '/';
    char *clean = xstrdup(rel);
    if (dir) clean[n - 1] = 0;
    if (!rel_ok(clean)) {
        free(clean);
        return -1;
    }
    char *abs = join_path(((fs_ctx *)ctx)->root, clean);
    free(clean);
    int rc = dir ? rmdir(abs) : unlink(abs);
    free(abs);
    return rc == 0 || errno == ENOENT ? 0 : -1;
}

/* ---- the request sigil wants --------------------------------------------------------------- */

typedef struct {
    game_t game;
    char *root;            /* the folder listed and written under (a layout with profiles re-roots it) */
    char *profile;
    strvec listing;
    sigil_save_option *opts;
    size_t opt_count;
    const char **ids;
    fs_ctx fs;
} req_t;

static void req_build(const args_t *a, req_t *q, sigil_save_request *sr) {
    memset(q, 0, sizeof(*q));
    memset(sr, 0, sizeof(*sr));
    char base[PATH_MAX], prof[SIGIL_PROFILE_ID_MAX];
    base[0] = prof[0] = 0;
    q->root = xstrdup(a->root);
    q->profile = a->profile ? xstrdup(a->profile) : NULL;
    if (sigil_save_base(a->layout, a->root, base, sizeof(base), prof, sizeof(prof)) == SIGIL_OK && base[0]) {
        free(q->root);
        q->root = xstrdup(base);
        if (!q->profile && prof[0]) q->profile = xstrdup(prof);
    }
    list_save_root(q->root, a->layout, &q->listing);
    if (!q->listing.items) { /* sigil wants an array even when the root holds nothing yet */
        q->listing.items = (char **)xmalloc(sizeof(char *));
        q->listing.items[0] = NULL;
        q->listing.cap = 1;
    }
    q->opt_count = a->option_keys.count;
    q->opts = (sigil_save_option *)xmalloc((q->opt_count + 1) * sizeof(*q->opts));
    for (size_t i = 0; i < q->opt_count; i++) {
        q->opts[i].key = a->option_keys.items[i];
        q->opts[i].value = a->option_values.items[i];
    }
    q->fs.root = q->root;
    sr->struct_version = SIGIL_SAVE_REQUEST_V1;
    sr->layout = a->layout;
    sr->content_path = a->content ? a->content : (a->rom ? a->rom : "");
    sr->options = q->opts;
    sr->option_count = q->opt_count;
    sr->listing = (const char *const *)q->listing.items;
    sr->listing_count = q->listing.count;
    sr->open = cb_open;
    sr->open_ctx = &q->fs;
    sr->root_path = q->root;
    sr->profile = q->profile;
}

static void req_free(req_t *q) {
    free(q->root);
    free(q->profile);
    free(q->opts);
    free(q->ids);
    sv_free(&q->listing);
}

static const char *need(const char *v, const char *name) {
    if (!v) die(2, "%s is required", name);
    return v;
}

/* ---- commands ----------------------------------------------------------------------------- */

static int cmd_version(void) {
    emit_begin(true);
    KV_STR("helper", HELPER_VERSION);
    KV_STR("sigil", sigil_version());
    KV_STR("sigil_pin", SIGIL_PIN);
    KV_STR("arch", HELPER_ARCH);
    KV_INT("pointer_bits", (int)(sizeof(void *) * 8));
    emit_end();
    return 0;
}

static void emit_game(const game_t *g) {
    const sigil_result *r = &g->result;
    KV_STR("platform", g->slug);
    KV_STR("title_id", r->title_id);
    KV_STR("raw_serial", r->raw_serial);
    KV_STR("save_id", r->save_id);
    KV_STR("usage", usage_name(r->usage));
    KV_STR("source", r->source == SIGIL_SOURCE_BINARY ? "binary" : "filename");
    KV_BOOL("experimental", r->experimental != 0);
    KV_INT("features", r->features);
    if (r->n64_md5[0]) {
        KV_STR("n64_header", r->n64_header);
        KV_STR("n64_md5", r->n64_md5);
        KV_STR("n64_md5_n64", r->n64_md5_n64);
    }
}

static int cmd_identify(const args_t *a) {
    need(a->rom, "--rom");
    game_t g;
    int rc = 0;
    if (identify(a, &g, &rc) != 0) return fail(rc, NULL);
    emit_begin(true);
    emit_game(&g);
    emit_end();
    return 0;
}

static int cmd_layouts(const args_t *a) {
    sigil_layout_info *rows = NULL;
    size_t n = 0;
    int rc = sigil_layouts(a->platform && strcmp(a->platform, "auto") ? a->platform : NULL, &rows, &n);
    if (rc != SIGIL_OK) return fail(rc, NULL);
    emit_begin(true);
    KEY("layouts");
    ARR();
    for (size_t i = 0; i < n; i++) {
        OBJ();
        KV_STR("id", rows[i].id);
        KV_STR("platform", rows[i].platform);
        KV_BOOL("profiles", rows[i].profiles != 0);
        KV_BOOL("needs_existing", rows[i].needs_existing != 0);
        KV_STR("region_option", rows[i].region_option);
        KEY("options");
        ARR();
        for (size_t o = 0; o < rows[i].option_count; o++) {
            const sigil_layout_option *op = &rows[i].options[o];
            OBJ();
            KV_STR("key", op->key);
            KV_STR("default", op->default_value);
            KEY("values");
            ARR();
            for (size_t v = 0; v < op->value_count; v++) jw_str(&W, op->values[v]);
            END_ARR();
            END_OBJ();
        }
        END_ARR();
        END_OBJ();
    }
    END_ARR();
    emit_end();
    sigil_layouts_free(rows);
    return 0;
}

static void emit_members(const char *name, const sigil_save_member *m, size_t n) {
    KEY(name);
    ARR();
    for (size_t i = 0; i < n; i++) {
        OBJ();
        KV_STR("path", m[i].path);
        KV_STR("entry", m[i].entry);
        KV_STR("role", role_name(m[i].role));
        KV_BOOL("present", m[i].present != 0);
        KV_STR("area", area_name(m[i].area));
        END_OBJ();
    }
    END_ARR();
}

static void emit_alternates(const sigil_save_alternate *alt, size_t n) {
    KEY("alternates");
    ARR();
    for (size_t i = 0; i < n; i++) {
        OBJ();
        KV_STR("path", alt[i].path);
        KV_BOOL("shared", alt[i].shared != 0);
        KEY("options");
        OBJ();
        for (size_t o = 0; o < alt[i].option_count; o++) KV_STR(alt[i].options[o].key, alt[i].options[o].value);
        END_OBJ();
        END_OBJ();
    }
    END_ARR();
}

static int cmd_locate(const args_t *a) {
    need(a->layout, "--layout");
    need(a->root, "--root");
    game_t g;
    int rc = 0;
    if (identify(a, &g, &rc) != 0) return fail(rc, NULL);
    req_t q;
    sigil_save_request sr;
    req_build(a, &q, &sr);
    sr.platform = g.slug[0] ? g.slug : NULL;
    sr.result = g.have ? &g.result : NULL;
    sr.features = g.result.features;
    sigil_save_unit *unit = NULL;
    rc = sigil_save_resolve(&sr, &unit);
    if (rc == SIGIL_OK && a->hash) rc = sigil_save_hash(unit, cb_open, &q.fs);
    if (rc != SIGIL_OK) {
        sigil_save_unit_free(unit);
        req_free(&q);
        return fail(rc, NULL);
    }
    emit_begin(true);
    KV_STR("root", q.root);
    KV_STR("key", unit->key);
    KV_STR("shape", shape_name(unit->shape));
    KV_STR("artifact", unit->artifact);
    KV_STR("content_hash", unit->content_hash);
    KV_STR("identity_hash", unit->identity_hash);
    emit_members("members", unit->members, unit->member_count);
    emit_members("expected", unit->expected, unit->expected_count);
    KEY("unkeyed");
    ARR();
    for (size_t i = 0; i < unit->unkeyed_count; i++) jw_str(&W, unit->unkeyed[i]);
    END_ARR();
    emit_alternates(unit->alternates, unit->alternate_count);
    emit_end();
    sigil_save_unit_free(unit);
    req_free(&q);
    return 0;
}

static int sync_mode(const args_t *a) {
    if (!a->mode || strcmp(a->mode, "unmanaged") == 0) return SIGIL_SYNC_UNMANAGED;
    if (strcmp(a->mode, "managed") == 0) return SIGIL_SYNC_MANAGED;
    die(2, "--mode is managed or unmanaged");
    return 0;
}

static int cmd_sync(const args_t *a, bool restore) {
    need(a->layout, "--layout");
    need(a->root, "--root");
    uint8_t *unit = NULL;
    size_t unit_len = 0;
    if (restore) {
        need(a->unit_in, "--unit");
        if (!read_file(a->unit_in, &unit, &unit_len)) return fail_msg(1, "io", "cannot read %s: %s", a->unit_in, strerror(errno));
    } else if (!a->unit_out) {
        die(2, "--out is required for collect");
    }
    game_t g;
    int rc = 0;
    if (identify(a, &g, &rc) != 0) {
        free(unit);
        return fail(rc, NULL);
    }
    uint8_t *state = NULL;
    size_t state_len = 0;
    if (a->state_in && !read_file(a->state_in, &state, &state_len)) {
        state = NULL;
        state_len = 0; /* no state yet is the normal first run */
    }

    req_t q;
    sigil_sync_request sq;
    memset(&sq, 0, sizeof(sq));
    sq.struct_version = SIGIL_SYNC_REQUEST_V1;
    req_build(a, &q, &sq.save);
    sq.save.platform = g.slug[0] ? g.slug : NULL;
    sq.save.result = g.have ? &g.result : NULL;
    sq.save.features = g.result.features;
    q.ids = (const char **)xmalloc((a->game_ids.count + 1) * sizeof(char *));
    for (size_t i = 0; i < a->game_ids.count; i++) q.ids[i] = a->game_ids.items[i];
    sq.game_ids = q.ids;
    sq.game_id_count = a->game_ids.count;
    sq.mode = sync_mode(a);
    sq.state = state;
    sq.state_len = state_len;
    sq.overwrite_local = a->overwrite_local ? 1 : 0;
    sq.repair = a->repair ? 1 : 0;
    sq.write = cb_write;
    sq.write_ctx = &q.fs;
    sq.remove = cb_remove;

    sigil_sync_result *r = NULL;
    rc = restore ? sigil_restore(&sq, unit, unit_len, &r) : sigil_collect(&sq, &r);
    int status = 0;
    if (rc != SIGIL_OK) {
        status = fail(rc, r);
    } else {
        bool wrote_unit = false, wrote_state = false;
        if (!restore && r->data && r->len) {
            if (write_atomic(a->unit_out, r->data, r->len) != 0) {
                status = fail_msg(1, "io", "cannot write %s: %s", a->unit_out, strerror(errno));
                goto done;
            }
            wrote_unit = true;
        }
        if (a->holding_out && r->holding && r->holding_len && write_atomic(a->holding_out, r->holding, r->holding_len) != 0) {
            status = fail_msg(1, "io", "cannot write %s: %s", a->holding_out, strerror(errno));
            goto done;
        }
        if (a->state_out && r->state && r->state_len) {
            if (write_atomic(a->state_out, r->state, r->state_len) != 0) {
                status = fail_msg(1, "io", "cannot write %s: %s", a->state_out, strerror(errno));
                goto done;
            }
            wrote_state = true;
        }
        emit_begin(true);
        KV_STR("op", restore ? "restore" : "collect");
        KV_STR("artifact", r->artifact);
        KV_STR("shape", shape_name(r->shape));
        KV_STR("content_hash", r->content_hash);
        KV_STR("identity_hash", r->identity_hash);
        KV_BOOL("changed", r->changed != 0);
        KV_BOOL("restore_again", r->restore_again != 0);
        KV_BOOL("hardcore_marker", r->hardcore_marker != 0);
        KV_BOOL("has_unit", wrote_unit);
        KV_INT("unit_len", wrote_unit ? r->len : 0);
        KV_BOOL("has_state", wrote_state);
        KV_INT("state_len", r->state_len);
        KV_BOOL("has_holding", r->holding && r->holding_len);
        KV_INT("unowned_changed", r->unowned_changed);
        KEY("unowned");
        ARR();
        for (size_t i = 0; i < r->unowned_count; i++) jw_str(&W, r->unowned[i]);
        END_ARR();
        KV_STR("profile", r->profile);
        KEY("profiles");
        ARR();
        for (size_t i = 0; i < r->profile_count; i++) {
            OBJ();
            KV_STR("id", r->profiles[i].id);
            KV_STR("name", r->profiles[i].name);
            END_OBJ();
        }
        END_ARR();
        emit_alternates(r->alternates, r->alternate_count);
        emit_end();
    }
done:
    sigil_sync_result_free(r);
    req_free(&q);
    free(unit);
    free(state);
    return status;
}

static int cmd_card_list(const args_t *a) {
    need(a->path, "--path");
    sigil_io *io = sigil_io_open_file(a->path);
    if (!io) return fail(SIGIL_ERR_IO, NULL);
    sigil_card_listing *l = NULL;
    int rc = sigil_card_list(io, &l);
    sigil_io_close(io);
    if (rc != SIGIL_OK) return fail(rc, NULL);
    static const char *formats[] = { "unknown", "ps1-raw", "ps1-gme", "ps1-vmp", "ps2", "gamecube-raw",
                                     "dreamcast-vmu", "saturn-backup", "segacd-bram" };
    emit_begin(true);
    KV_STR("format", l->format >= 0 && l->format < 9 ? formats[l->format] : "unknown");
    KV_INT("total_blocks", l->total_blocks);
    KV_INT("free_blocks", l->free_blocks);
    KV_INT("free_slots", l->free_slots);
    KV_INT("corrupt_count", l->corrupt_count);
    for (int pass = 0; pass < 2; pass++) {
        KEY(pass ? "corrupt_entries" : "entries");
        ARR();
        const sigil_card_entry *e = pass ? l->corrupt_entries : l->entries;
        size_t n = pass ? l->corrupt_entry_count : l->entry_count;
        for (size_t i = 0; i < n; i++) {
            OBJ();
            KV_STR("name", e[i].name);
            KV_STR("owner_id", e[i].owner_id);
            KV_INT("blocks", e[i].blocks);
            KV_INT("first_block", e[i].first_block);
            END_OBJ();
        }
        END_ARR();
    }
    emit_end();
    sigil_card_listing_free(l);
    return 0;
}

/* Not in sigil.h: the superblock of a freshly formatted PCSX2 folder card (8 KiB). The pinned sigil
 * build exports it, and the link fails loudly if a newer sigil drops it. */
#define FOLDER_SUPERBLOCK_SIZE 0x2000u
extern void sigil_ps2_folder_superblock(uint8_t out[FOLDER_SUPERBLOCK_SIZE]);

static bool dir_has_entries(const char *dir) {
    strvec names = { 0 };
    dir_names(dir, &names);
    bool any = names.count > 0;
    sv_free(&names);
    return any;
}

/* sigil's own test for a formatted superblock: full length, and the 'o' of "Format" in the
 * "Sony PS2 Memory Card Format" magic at offset 0x16 (card_ps2.c, PS2_FORMATTED_AT). */
static bool superblock_usable(const char *path) {
    uint8_t *b = NULL;
    size_t n = 0;
    if (!read_file(path, &b, &n)) return false;
    bool ok = n >= FOLDER_SUPERBLOCK_SIZE && b[0x16] == 0x6F;
    free(b);
    return ok;
}

/* Makes `memcards/<name>` a PCSX2 folder card when nothing is there yet, and says what it found
 * otherwise. It never replaces a card: a file card, or a folder holding saves, is reported as it is. */
static int cmd_init_folder_card(const args_t *a) {
    need(a->root, "--root");
    need(a->path, "--path"); /* the card's name, e.g. Mcd001.ps2 */
    const char *dir = a->layout ? a->layout : "memcards"; /* --layout doubles as the memcards folder here */
    if (!rel_ok(dir) || !rel_ok(a->path) || strchr(a->path, '/')) return fail_msg(2, "invalid_arg", "bad card name or folder");
    char *memcards = join_path(a->root, dir);
    char *card = join_path(memcards, a->path);
    char *sb = join_path(card, "_pcsx2_superblock");
    const char *status = "created";
    struct stat st;
    int status_code = 0;
    if (stat(card, &st) == 0) {
        if (S_ISREG(st.st_mode)) {
            status = "is_file";
        } else if (S_ISDIR(st.st_mode)) {
            if (superblock_usable(sb)) status = "exists";
            else if (dir_has_entries(card) && !is_file(sb)) status = "unformatted_with_saves";
            else status = "formatted";
        } else {
            status = "unusable";
        }
    } else if (mkdir_p(memcards) != 0 || mkdir(card, 0777) != 0) {
        status = "io";
        status_code = 1;
    }
    if (!strcmp(status, "created") || !strcmp(status, "formatted")) {
        uint8_t block[FOLDER_SUPERBLOCK_SIZE];
        sigil_ps2_folder_superblock(block);
        if (write_atomic(sb, block, sizeof(block)) != 0) {
            status = "io";
            status_code = 1;
        }
    }
    emit_begin(status_code == 0);
    KV_STR("status", status);
    KV_STR("path", card);
    KV_STR("kind", !strcmp(status, "is_file") ? "file" : "folder");
    emit_end();
    free(memcards);
    free(card);
    free(sb);
    return status_code;
}

int main(int argc, char **argv) {
    args_t a;
    memset(&a, 0, sizeof(a));
    parse_args(argc, argv, &a);
    const char *c = a.cmd;
    if (strcmp(c, "version") == 0) return cmd_version();
    if (strcmp(c, "identify") == 0) return cmd_identify(&a);
    if (strcmp(c, "layouts") == 0) return cmd_layouts(&a);
    if (strcmp(c, "locate") == 0) return cmd_locate(&a);
    if (strcmp(c, "collect") == 0) return cmd_sync(&a, false);
    if (strcmp(c, "restore") == 0) return cmd_sync(&a, true);
    if (strcmp(c, "card-list") == 0) return cmd_card_list(&a);
    if (strcmp(c, "init-folder-card") == 0) return cmd_init_folder_card(&a);
    die(2, "unknown command '%s'", c);
    return 2;
}
