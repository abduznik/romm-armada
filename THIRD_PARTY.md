# Third-party code

## argosy-sigil (MPL-2.0)

`bin/savebridge-aarch64` and `bin/savebridge-x86_64` are built from this plugin's own front end
(`native/savebridge/savebridge.c`, MIT) linked statically with
[argosy-sigil](https://github.com/rommapp/argosy-sigil), a C library by the RomM project that reads a game's
id out of its disc image and moves one game's saves between an emulator and RomM.

- Revision: `5d4ddb41cc22cc017e32cccf4ca08dea270cbe40` (pinned in `native/SIGIL_PIN`)
- License: Mozilla Public License 2.0, text in `licenses/argosy-sigil-LICENSE-MPL-2.0.txt`
- The sigil files are **unmodified**. The complete source of the exact version that was built is the pinned
  revision of the repository above, and `native/build.sh` rebuilds the two executables from it.
- The name "argosy-sigil" belongs to its project (see its `TRADEMARKS.md`). This plugin only uses the library; it is
  not a fork and is not made or endorsed by the RomM or Argosy projects.

sigil bundles, and so the helper contains:

| Component | License | Where |
|---|---|---|
| libchdr (CHD reader) | BSD-3-Clause | `third_party/libchdr` of sigil; `licenses/libchdr-LICENSE.txt` |
| zstd 1.5.6 (decompressor) | BSD-3-Clause | inside libchdr |
| zlib 1.3.1 | zlib license | inside libchdr |
| LZMA SDK 24.05 | public domain | inside libchdr |
| tiny-AES-c | Unlicense | `third_party/tiny-AES-c` of sigil |
| buram (Sega CD backup RAM layout table) | MIT, (c) 2022 Ian Karlsson | `licenses/buram-MIT.txt` |

The executables are statically linked against glibc (LGPL-2.1+) from Debian 12; the object code needed to relink
them is reproducible with `native/build.sh`.

## Decky Loader

The panel is built against `@decky/api` and `@decky/ui` (MIT / LGPL-2.1, from the Decky Loader project) and runs
inside Decky Loader. The build is bundled by `@decky/rollup`.

## Reference material

How RomM's API behaves, how Armada OS lays out its emulators, and where ARMSX2 keeps its data was read from those
projects' public source code. No code from them is included.
