#!/usr/bin/env bash
export CC=$(which x86_64-conda-linux-gnu-gcc)
export CXX=$(which x86_64-conda-linux-gnu-g++)
export AR=$(which x86_64-conda-linux-gnu-ar)
export RANLIB=$(which x86_64-conda-linux-gnu-ranlib)
export PKG_CONFIG_PATH="$CONDA_PREFIX/lib/pkgconfig"
export CMAKE_PREFIX_PATH="$CONDA_PREFIX"
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:$LD_LIBRARY_PATH"
