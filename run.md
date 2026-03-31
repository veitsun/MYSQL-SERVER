cd /home/sunwei/mysql-server
RUN_BASE=$PWD/build/devrun
DATADIR=$RUN_BASE/data
SOCKET=$RUN_BASE/mysql.sock
PORT=3406

rm -rf "$RUN_BASE"
mkdir -p "$DATADIR"

# 初始化数据目录（root 空密码，仅开发环境）
build/runtime_output_directory/mysqld \
  --no-defaults \
  --basedir=$PWD/build \
  --datadir="$DATADIR" \
  --initialize-insecure

# 后台启动
build/runtime_output_directory/mysqld \
  --no-defaults \
  --basedir=$PWD/build \
  --datadir="$DATADIR" \
  --socket="$SOCKET" \
  --port=$PORT \
  --pid-file="$RUN_BASE/mysql.pid" \
  --log-error="$RUN_BASE/error.log" \
  --daemonize

# 连接验证
build/runtime_output_directory/mysql --no-defaults -uroot -S "$SOCKET" \
  -e "SELECT VERSION(), @@port, @@socket;"
