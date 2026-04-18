# 在 235 机器上

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
  -DWITH_NCURSES=system \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON

```

3）编译

```bash
ninja -C build -j$(nproc)
```


---

# 在 126 机器上

1) 准备 Boost 到项目根目录
```bash
rm -rf boost
mkdir -p boost
curl -L https://archives.boost.io/release/1.77.0/source/boost_1_77_0.tar.bz2 \
  -o boost/boost_1_77_0.tar.bz2
tar -xjf boost/boost_1_77_0.tar.bz2 -C boost
```

2) 清理旧构建目录
```bash
rm -rf build install
```

3) CMake 配置
```bash
cmake -S . -B build -G Ninja \
  -DCMAKE_C_COMPILER=gcc-8 \
  -DCMAKE_CXX_COMPILER=g++-8 \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DWITH_DEBUG=1 \
  -DWITH_GROUP_REPLICATION=OFF \
  -DWITHOUT_FEDERATED_STORAGE_ENGINE=ON \
  -DWITH_BOOST=$PWD/boost/boost_1_77_0 \
  -DWITH_SSL=system \
  -DWITH_ZLIB=bundled \
  -DWITH_ZSTD=system \
  -DWITH_LZ4=system \
  -DWITH_ICU=bundled \
  -DWITH_READLINE=system \
  -DWITH_NCURSES=system \
  -DCMAKE_INSTALL_PREFIX=$PWD/install \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```

4) 编译（包含链接）
```bash
ninja -C build -j"$(nproc)"
```

---

# NUMA Frame 统计 CSV（新增插桩）

这个插桩在 `InnoDB buffer pool` 释放前（即服务关闭流程）执行：

- 遍历每个 `frame`（`buf_block_t::frame`）
- 使用 `move_pages()` 查询该 frame 覆盖的 OS 页所在 NUMA node
- 结果输出到 CSV 文件

输出文件路径：

- 默认写到 `innodb_data_home_dir`（通常和 datadir 一致）
- 文件名：`innodb_buffer_frame_numa.csv`

CSV 字段：

- `buf_pool_instance`: buffer pool 实例编号
- `chunk_index`: chunk 编号
- `block_index`: chunk 内 block 编号
- `frame_address`: frame 起始地址
- `block_state`: frame 当前状态（如 `BUF_BLOCK_FILE_PAGE`）
- `os_page_size`: OS 页大小（如 4096）
- `innodb_page_size`: InnoDB 页大小（通常 16384）
- `numa_nodes`: 该 frame 涉及的 NUMA node 列表（`;` 分隔）
- `numa_node_page_counts`: 每个 node 覆盖页数（格式如 `0:3;1:1`）
- `unknown_page_count`: 查询失败或未知页数量
- `query_error`: `move_pages()` 的 errno（0 表示成功）

注意：

- 该功能在编译启用 `HAVE_LIBNUMA` 时生效。
- 如果系统 NUMA 不可用，关闭时会跳过导出并记录告警。

# 最小验证 （默认启动）

```bash
cd /home/xmu/MYSQL-SERVER

RUN_BASE=$PWD/build/numa_probe_run
DATADIR=$RUN_BASE/data
SOCKET=$RUN_BASE/mysql.sock
PORT=3407

rm -rf "$RUN_BASE"
mkdir -p "$DATADIR"

# 初始化
build/runtime_output_directory/mysqld \
  --no-defaults \
  --basedir=$PWD/build \
  --datadir="$DATADIR" \
  --initialize-insecure

# 启动
build/runtime_output_directory/mysqld \
  --no-defaults \
  --basedir=$PWD/build \
  --datadir="$DATADIR" \
  --socket="$SOCKET" \
  --port=$PORT \
  --pid-file="$RUN_BASE/mysql.pid" \
  --log-error="$RUN_BASE/error.log" \
  --innodb_buffer_pool_size=256M \
  --daemonize

# 造一点 InnoDB 访问
build/runtime_output_directory/mysql --no-defaults -uroot -S "$SOCKET" -e "
CREATE DATABASE IF NOT EXISTS numa_demo;
USE numa_demo;
CREATE TABLE t(id INT PRIMARY KEY AUTO_INCREMENT, c1 VARCHAR(200), c2 INT) ENGINE=InnoDB;
INSERT INTO t(c1,c2)
SELECT REPEAT('x',100), seq FROM (
  SELECT 1 AS seq UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5
  UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9 UNION ALL SELECT 10
) s;
SELECT COUNT(*) FROM t;
"

# 触发导出（关键）
build/runtime_output_directory/mysqladmin --no-defaults -uroot -S "$SOCKET" shutdown

```


# 强验证（NUMA 对照实验）
用 numactl 把 mysqld 绑到 node1，再看 CSV 中 numa_nodes 是否偏向 1：
```bash
cd /home/xmu/MYSQL-SERVER

RUN_BASE=$PWD/build/numa_probe_run_node1
DATADIR=$RUN_BASE/data
SOCKET=$RUN_BASE/mysql.sock
PORT=3408

rm -rf "$RUN_BASE"
mkdir -p "$DATADIR"

build/runtime_output_directory/mysqld \
  --no-defaults \
  --basedir=$PWD/build \
  --datadir="$DATADIR" \
  --initialize-insecure

numactl --cpunodebind=1 --membind=1 \
build/runtime_output_directory/mysqld \
  --no-defaults \
  --basedir=$PWD/build \
  --datadir="$DATADIR" \
  --socket="$SOCKET" \
  --port=$PORT \
  --pid-file="$RUN_BASE/mysql.pid" \
  --log-error="$RUN_BASE/error.log" \
  --innodb_buffer_pool_size=256M \
  --daemonize

build/runtime_output_directory/mysql --no-defaults -uroot -S "$SOCKET" -e "SELECT 1;"

build/runtime_output_directory/mysqladmin --no-defaults -uroot -S "$SOCKET" shutdown

```

# 开启 interleave
这份源码里 innodb_numa_interleave 是 READONLY， 所以要在 mysqld 启动设置
1、临时开启（单次启动）
```bash
build/runtime_output_directory/mysqld \
  --no-defaults \
  --basedir=$PWD/build \
  --datadir="$DATADIR" \
  --socket="$SOCKET" \
  --port=$PORT \
  --pid-file="$RUN_BASE/mysql.pid" \
  --log-error="$RUN_BASE/error.log" \
  --innodb_buffer_pool_size=256M \
  --innodb-numa-interleave=ON \
  --daemonize

```

2、持久开启（配置文件）
my.cnf 的 [mysqld] 文件 下加：
```bash
[mysqld]
innodb_numa_interleave=ON
```

# linux 服务器开启关闭 autoNUMA

1、查看当前状态
```bash
sysctl kernel.numa_balancing
# 或
cat /proc/sys/kernel/numa_balancing
```

2、临时开启/关闭（重启后失效）
- 开启
```bash
sysctl -w kernel.numa_balancing=1
# 或
echo 1 > /proc/sys/kernel/numa_balancing
```
- 关闭
```bash
sysctl -w kernel.numa_balancing=0
# 或
echo 0 > /proc/sys/kernel/numa_balancing
```

---

# 86GB 数据量 + 86GB Buffer Pool 的 sysbench 观测实验

这条命令用于跑单个 `baseline` case，目标是：

- `sysbench` 目标数据量约 `86GiB`（通过 `--target-data-gib 86` 自动换算 `table_size`）
- `InnoDB buffer pool` 大小设为 `86G`
- 运行目录放到大盘路径 `/home/hdd/xmu/numa_runs_86g`

执行命令：

```bash
cd /home/xmu/MYSQL-SERVER
./run_numa_sysbench_observe.sh \
  --cases baseline \
  --run-root /home/hdd/xmu/numa_runs_86g \
  --target-data-gib 86 \
  --buffer-pool-size 86G
```

参数说明：

- `--cases baseline`：只跑 1 组基线实验，避免一次跑 4 组占用过大空间和时间。
- `--run-root /home/hdd/xmu/numa_runs_86g`：结果目录，建议放在大容量磁盘。
- `--target-data-gib 86`：每个 case 的目标数据量（估算值），脚本会自动计算 `--table-size`。
- `--buffer-pool-size 86G`：启动 mysqld 时设置 `--innodb_buffer_pool_size=86G`。

执行前检查：

1) 确认结果目录可写（否则会直接失败）
```bash
test -w /home/hdd/xmu && echo "writable" || echo "not writable"
```

2) 确认大盘剩余空间足够（建议预留 > 110GiB）
```bash
df -h /home/hdd
```

说明：脚本内部会做一次磁盘空间预检查，如果空间不足会提前报错退出，不会跑到中途失败。

实验输出目录结构（关键文件）：

- `/home/hdd/xmu/numa_runs_86g/baseline/sysbench_prepare.log`
- `/home/hdd/xmu/numa_runs_86g/baseline/sysbench_run.log`
- `/home/hdd/xmu/numa_runs_86g/baseline/error.log`
- `/home/hdd/xmu/numa_runs_86g/baseline/data/innodb_buffer_frame_numa.csv`
- `/home/hdd/xmu/numa_runs_86g/baseline/numa_summary.txt`
- `/home/hdd/xmu/numa_runs_86g/experiment_config.txt`

推荐关注：

- `experiment_config.txt`：确认脚本实际计算出的 `table_size`、估算数据量和 buffer pool 配置。
- `numa_summary.txt`：快速看 `BUF_BLOCK_FILE_PAGE` 的 NUMA 节点分布统计。
- `innodb_buffer_frame_numa.csv`：后续可视化分析的原始数据。
