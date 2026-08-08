cmake_minimum_required(VERSION 3.20)
project(sdisco C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

set(CMAKE_C_FLAGS_RELEASE "-O3")

find_library(HACKRF_LIB hackrf REQUIRED)
find_library(LIQUID_LIB liquid REQUIRED)
find_library(PORTAUDIO_LIB portaudio REQUIRED)

find_package(Threads REQUIRED)

include_directories(/opt/homebrew/include)
include_directories(/opt/homebrew/include/libhackrf)

add_executable(sdisco
  src/main.c
  src/channel.c
  src/hackrx.c
  src/dsp.c
  src/audio.c
  src/ring.c
)

target_include_directories(sdisco PRIVATE src /opt/homebrew/include)
target_link_libraries(sdisco
  ${HACKRF_LIB}
  ${LIQUID_LIB}
  ${PORTAUDIO_LIB}
  Threads::Threads
)

target_compile_options(sdisco PRIVATE -Wall -Wextra)

add_executable(sim_test
  tests/sim_test.c
  src/channel.c
  src/dsp.c
  src/ring.c
)

target_include_directories(sim_test PRIVATE src tests /opt/homebrew/include)
target_link_libraries(sim_test ${LIQUID_LIB} Threads::Threads)
target_compile_options(sim_test PRIVATE -Wall -Wextra)
