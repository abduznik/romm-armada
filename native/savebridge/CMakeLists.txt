# SPDX-License-Identifier: MIT
cmake_minimum_required(VERSION 3.16)
project(savebridge C)

set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

set(SIGIL_SRC "" CACHE PATH "Checkout of argosy-sigil, submodules included")
set(SIGIL_PIN "unknown" CACHE STRING "The sigil commit this build is made from")
option(SB_STATIC "Link the helper statically, so it needs no particular glibc" ON)

if(NOT SIGIL_SRC OR NOT EXISTS "${SIGIL_SRC}/CMakeLists.txt")
    message(FATAL_ERROR "Pass -DSIGIL_SRC=<argosy-sigil checkout>")
endif()

set(SIGIL_BUILD_CLI OFF CACHE BOOL "" FORCE)
set(SIGIL_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(SIGIL_BUILD_SHARED OFF CACHE BOOL "" FORCE)
add_subdirectory("${SIGIL_SRC}" sigil EXCLUDE_FROM_ALL)

add_executable(savebridge savebridge.c)
target_link_libraries(savebridge PRIVATE sigil)
target_compile_definitions(savebridge PRIVATE
    _POSIX_C_SOURCE=200809L _DEFAULT_SOURCE _FILE_OFFSET_BITS=64 SIGIL_PIN="${SIGIL_PIN}")
target_compile_options(savebridge PRIVATE -Wall -Wextra -Wno-unused-parameter -fstack-protector-strong)
if(SB_STATIC)
    target_link_options(savebridge PRIVATE -static)
endif()
