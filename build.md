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

RUN_BASE=$PWD/build/numa_probe_run6
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

# instance interleave 启动
build/runtime_output_directory/mysqld \
  --no-defaults \
  --basedir=$PWD/build \
  --datadir="$DATADIR" \
  --socket="$SOCKET" \
  --port=$PORT \
  --pid-file="$RUN_BASE/mysql.pid" \
  --log-error="$RUN_BASE/error.log" \
  --innodb_buffer_pool_instances=8 \
  --innodb_buffer_pool_size=2G \
  --innodb-numa-interleave-instance=ON \
  --daemonize

# os page interleave 启动
build/runtime_output_directory/mysqld \
  --no-defaults \
  --basedir=$PWD/build \
  --datadir="$DATADIR" \
  --socket="$SOCKET" \
  --port=$PORT \
  --pid-file="$RUN_BASE/mysql.pid" \
  --log-error="$RUN_BASE/error.log" \
  --innodb_buffer_pool_instances=8 \
  --innodb_buffer_pool_size=2G \
  --innodb-numa-interleave=ON \
  --daemonize

# fist-touch 启动
build/runtime_output_directory/mysqld \
  --no-defaults \
  --basedir=$PWD/build \
  --datadir="$DATADIR" \
  --socket="$SOCKET" \
  --port=$PORT \
  --pid-file="$RUN_BASE/mysql.pid" \
  --log-error="$RUN_BASE/error.log" \
  --innodb_buffer_pool_instances=8 \
  --innodb_buffer_pool_size=2G \
  --daemonize

# database page interleave

# 造一点 InnoDB 访问（这是真一点点数据，只插入 10 行数据）
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

# 造更多的数据，大概在 1.5 G 数据量
build/runtime_output_directory/mysql --no-defaults -uroot -S "$SOCKET" -e "
CREATE DATABASE IF NOT EXISTS numa_demo;
USE numa_demo;
DROP TABLE IF EXISTS t;
CREATE TABLE t(
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  c1 VARCHAR(1000),
  c2 INT
) ENGINE=InnoDB;

INSERT INTO t(c1, c2)
SELECT REPEAT('x', 1000), seq
FROM (
  SELECT
    a.i
    + b.i * 10
    + c.i * 100
    + d.i * 1000
    + e.i * 10000
    + f.i * 100000
    + g.i * 1000000
    + 1 AS seq
  FROM
    (SELECT 0 i UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) a
    CROSS JOIN
    (SELECT 0 i UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) b
    CROSS JOIN
    (SELECT 0 i UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) c
    CROSS JOIN
    (SELECT 0 i UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) d
    CROSS JOIN
    (SELECT 0 i UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) e
    CROSS JOIN
    (SELECT 0 i UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) f
    CROSS JOIN
    (SELECT 0 i UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9) g
) AS x
WHERE seq <= 2000000;

SELECT COUNT(*) AS row_count FROM t;
"

# 随机读写压测脚本（相当于 sysbench 的 run 阶段）

前面的建表和造数步骤相当于 `sysbench prepare`。下面这个脚本直接对已经存在的 `numa_demo.t` 做随机读写压测，不需要重新初始化表。

脚本路径：

```bash
scripts/numa_random_rw_bench.py
```

默认行为：

- 随机点查 `id`
- 做小范围 `BETWEEN` 查询
- 事务内随机执行 `UPDATE`、`INSERT`、`DELETE`
- 多线程并发输出吞吐和延迟

先确认 `SOCKET` 变量已经设置好，然后直接运行：

```bash
python3 scripts/numa_random_rw_bench.py \
  --mysql-bin build/runtime_output_directory/mysql \
  --socket "$SOCKET" \
  --database numa_demo \
  --table t \
  --threads 8 \
  --time 300 \
  --report-interval 5 \
  --read-ratio 0.8
```

参数说明：

- `--threads`：并发线程数，类似 sysbench 的 `--threads`
- `--time`：压测时长，单位秒
- `--report-interval`：输出间隔
- `--read-ratio`：读事务占比，例如 `0.8` 表示 80% 读、20% 写
- `--point-selects`：每个读事务里的随机点查次数
- `--range-size`：范围查询的跨度
- `--update-pct`、`--insert-pct`、`--delete-pct`：写事务内部的操作比例，三者之和必须等于 `1.0`

如果你想更接近 sysbench 的 `oltp_read_write`，可以用：

```bash
python3 scripts/numa_random_rw_bench.py \
  --mysql-bin build/runtime_output_directory/mysql \
  --socket "$SOCKET" \
  --database numa_demo \
  --table t \
  --threads 16 \
  --time 600 \
  --report-interval 10 \
  --read-ratio 0.8 \
  --point-selects 10 \
  --range-size 100 \
  --update-pct 0.7 \
  --insert-pct 0.15 \
  --delete-pct 0.15
```

说明：

- 这个脚本不依赖 `PyMySQL` 或 `mysql-connector`，只使用仓库自带的 `mysql` 客户端。
- 如果 `--socket` 为空，会直接报错，所以必须先确保 `SOCKET` 环境变量正确。
- 压测结束后，仍然用 `mysqladmin --no-defaults -uroot -S "$SOCKET" shutdown` 正常关闭 mysqld。

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

# innodb_numa_interleave_instance — Buffer Pool Instance 级 NUMA 绑定

## 背景

原有的 `innodb_numa_interleave` 使用 `MPOL_INTERLEAVE` 策略，以 4KB OS 页为粒度在所有 NUMA node 间轮询分配内存。这导致一个 16KB 的 InnoDB page 被切成 4 片分散在不同 node 上，读取一个 page 最多需要跨 4 个 node 取数据。

`innodb_numa_interleave_instance` 改为以 **buffer pool instance** 为粒度，将每个 instance 的内存整体绑定到一个固定的 NUMA node（`MPOL_BIND`），按 `instance_no % num_nodes` 轮询分配。这样每个 16KB page 物理上完整地位于一个 node，消除了 intra-page 跨 node 访问。

## 涉及文件

| 文件 | 改动内容 |
|------|---------|
| `storage/innobase/include/srv0srv.h` | 新增 `extern bool srv_numa_interleave_instance` 声明 |
| `storage/innobase/srv/srv0srv.cc` | 新增变量定义，默认 `false` |
| `storage/innobase/handler/ha_innodb.cc` | 注册 `MYSQL_SYSVAR_BOOL(numa_interleave_instance, ...)` 并加入 sysvar 数组 |
| `storage/innobase/buf/buf0buf.cc` | `buf_pool_t::allocate_chunk` 中新增 `else if` 分支，执行 `mbind(MPOL_BIND)` |

## 使用方式

两个参数互斥，同时开启时 `innodb_numa_interleave` 优先。

**临时开启（单次启动）：**
```bash
build/runtime_output_directory/mysqld \
  --no-defaults \
  --innodb_buffer_pool_instances=8 \
  --innodb_buffer_pool_size=256M \
  --innodb-numa-interleave-instance=ON \
  ...
```

**持久开启（配置文件）：**
```ini
[mysqld]
innodb_numa_interleave_instance = ON
```

## 注意事项

- 需要编译时启用 libnuma（`-DWITH_NUMA=ON` 或系统已安装 `libnuma-dev`），否则该参数不会编译进去（整个逻辑在 `#ifdef HAVE_LIBNUMA` 内）。
- 建议将 `innodb_buffer_pool_instances` 设置为 NUMA node 数量的整数倍，以保证各 node 分配到相同数量的 instance，避免内存不均衡。
- 该参数为 `PLUGIN_VAR_READONLY`，只能在启动时设置，运行时不可更改。
- 绑定时使用 `MPOL_MF_MOVE` 标志，会将已分配但尚未物理落页的内存迁移到目标 node，启动时 error log 中会打印每个 instance 的绑定信息。

## linux 服务器开启关闭 autoNUMA

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

# NUMA 监控脚本

## scripts/numa_monitor.py（推荐使用）

统一 NUMA 监控工具，将两类指标合并在一张图上：

| 线条 | 颜色 | 数据来源 | 含义 |
|------|------|---------|------|
| 蓝色虚线 | blue dashed | `/proc/vmstat` `numa_hint_faults` | AutoNUMA 采样的运行时跨节点访问率（需 `kernel.numa_balancing=1`） |
| 绿色点线 | green dotted | `/proc/vmstat` `numa_local` / `numa_other` | 新页分配时的跨节点率（仅 buffer pool 初始化阶段非零） |

图中还有三条水平均值线：

- **绿色实线**：绿线非零阶段（buffer pool 初始化期）的平均分配远端率
- **蓝色点划线**：绿线归零之后（稳态压测阶段）蓝线的平均访问率
- **深蓝色点线**：蓝线整体平均访问率

### 用法

```bash
# 一次性快照（绝对计数）
python3 scripts/numa_monitor.py --pid <mysqld-pid> --once

# 持续监控，5 秒采样，60 秒后自动绘图退出
python3 scripts/numa_monitor.py --pid <mysqld-pid> \
    --interval 5 --duration 60 --output ./numa_profile/combined.csv

# 通过 pid 文件（可在 mysqld 启动前提前运行，脚本等待最多 30 秒）
python3 scripts/numa_monitor.py --pid-file "$RUN_BASE/mysql.pid" \
    --interval 5 --duration 120 --output ./numa_profile/combined.csv
```

> **注意**：要捕获 buffer pool 初始化阶段的绿线（分配局部性），必须在 mysqld 启动前或启动后立即运行脚本。推荐使用 `--pid-file` 模式，脚本会自动等待 pid 文件出现（最多 30 秒）。

输出文件：

- `<output>.csv`：每个采样点的原始数据
- `<output>_plot.png`：时序折线图（需 matplotlib；无 matplotlib 时退化为 ASCII 图）

### 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--pid` | — | mysqld 进程 PID（与 `--pid-file` 二选一） |
| `--pid-file` | — | mysql.pid 文件路径，脚本等待文件出现后读取 PID |
| `--once` | — | 打印一次绝对计数快照后退出 |
| `--interval` | 5.0 | 采样间隔（秒） |
| `--duration` | 无限 | 运行时长（秒），到达后自动绘图退出 |
| `--output` | 不保存 | CSV 输出路径，目录不存在时自动创建 |

### CSV 字段

| 字段 | 说明 |
|------|------|
| `timestamp` | 采样时刻（`YYYY-MM-DD HH:MM:SS`） |
| `elapsed_s` | 距监控启动的秒数 |
| `local_node` | `/sys/.../numastat` `local_node` 增量 |
| `other_node` | `/sys/.../numastat` `other_node` 增量 |
| `node_remote_pct` | `other_node / (local_node + other_node) × 100` |
| `hint_faults` | AutoNUMA `numa_hint_faults` 增量 |
| `hint_local` | AutoNUMA `numa_hint_faults_local` 增量 |
| `hint_remote_pct` | `(hint_faults - hint_local) / hint_faults × 100` |
| `alloc_local` | `vmstat numa_local` 增量 |
| `alloc_other` | `vmstat numa_other` 增量 |
| `alloc_remote_pct` | `alloc_other / (alloc_local + alloc_other) × 100` |
| `pages_migrated` | AutoNUMA 迁移页数增量 |

---

## scripts/numa_stat_monitor.py（分配局部性）

专注于 `/proc/PID/numa_maps`（物理页分布）和 `/proc/vmstat` `numa_local`/`numa_other`（分配时局部性）。适合在 mysqld 启动阶段观察 buffer pool 内存落在哪些 NUMA 节点上。

```bash
python3 scripts/numa_stat_monitor.py --pid <mysqld-pid> \
    --interval 5 --duration 60 --output ./numa_profile/alloc.csv

# 只看大匿名段（buffer pool，默认阈值 64 MiB）
python3 scripts/numa_stat_monitor.py --pid <mysqld-pid> --bp-only --bp-min-mb 64
```

---

## scripts/numa_runtime_monitor.py（运行时访问局部性）

专注于 `/sys/devices/system/node/nodeX/numastat` 和 AutoNUMA hint faults，不读取 numa_maps。适合在稳态压测阶段长时间采样。

```bash
python3 scripts/numa_runtime_monitor.py --pid <mysqld-pid> \
    --interval 5 --duration 300 --output ./numa_profile/runtime.csv
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
