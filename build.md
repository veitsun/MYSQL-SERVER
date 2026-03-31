1）清缓存

```bash
rm -rf ./build/CMakeCache.txt ./build/CMakeFiles

```

2）构建

```bash

cmake -S . -B build -G Ninja \
  -DCMAKE_C_COMPILER=x86_64-conda-linux-gnu-gcc \
  -DCMAKE_CXX_COMPILER=x86_64-conda-linux-gnu-g++ \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DWITH_DEBUG=1 \
  -DWITH_GROUP_REPLICATION=OFF \
  -DWITHOUT_FEDERATED_STORAGE_ENGINE=ON \
  -DWITH_BOOST=./boost/boost_1_77_0 \
  -DCMAKE_INSTALL_PREFIX=$PWD/install \
  -DWITH_SSL=system \
  -DWITH_ZLIB=system \
  -DWITH_ZSTD=system \
  -DWITH_LZ4=system \
  -DWITH_ICU=system \
  -DWITH_READLINE=system \
  -DWITH_NCURSES=system

```

3）编译

```bash
ninja -C build -j$(nproc)
```


