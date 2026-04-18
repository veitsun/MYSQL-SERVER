#!/usr/bin/env bash
set -euo pipefail

show_help() {
  cat <<'EOF'
Usage:
  ./run_numa_sysbench_observe.sh [options]

Options:
  --build-dir DIR             Build directory (default: ./build)
  --run-root DIR              Output root directory
                              (default: ./build/numa_sysbench_runs/<timestamp>)
  --cases LIST                Comma-separated case names (default: baseline,interleave_on,bind_node0,bind_node1)
                              Supported: baseline, interleave_on, bind_node0, bind_node1
  --base-port N               First MySQL port (default: 3406)
  --buffer-pool-size SIZE     InnoDB buffer pool size (default: 256M)
  --db-name NAME              Sysbench database name (default: sbtest)
  --tables N                  Sysbench tables (default: 16)
  --table-size N              Rows per table (manual override; disables auto sizing)
  --target-data-gib N         Target data volume per case in GiB for sbtest payload+index estimate
                              (default: 86, only used when --table-size is not provided)
  --prepare-threads N         Threads used by prepare (default: 8)
  --run-threads N             Threads used by run (default: 64)
  --run-time SEC              Duration of run phase (default: 300)
  --report-interval SEC       Sysbench report interval (default: 5)
  --skip-prepare              Skip sysbench prepare phase
  --do-cleanup                Run sysbench cleanup phase after run (default: off)
  --skip-cleanup              Force disable cleanup phase
  --help                      Show this help

Examples:
  ./run_numa_sysbench_observe.sh --cases baseline --run-root /home/hdd/numa_runs
  ./run_numa_sysbench_observe.sh --cases baseline,interleave_on --run-time 120
  ./run_numa_sysbench_observe.sh --cases bind_node1 --run-threads 128 --target-data-gib 86
EOF
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $1" >&2
    exit 1
  fi
}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$ROOT_DIR/build"
RUN_ROOT=""
CASES_CSV="baseline,interleave_on,bind_node0,bind_node1"
BASE_PORT=3406
BUFFER_POOL_SIZE="256M"
DB_NAME="sbtest"
TABLES=16
TABLE_SIZE=0
TABLE_SIZE_MANUAL=0
TARGET_DATA_GIB=86
ROW_BYTES_EST=225
PREPARE_THREADS=8
RUN_THREADS=64
RUN_TIME=300
REPORT_INTERVAL=5
DO_PREPARE=1
DO_CLEANUP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-dir)
      BUILD_DIR="$2"
      shift 2
      ;;
    --run-root)
      RUN_ROOT="$2"
      shift 2
      ;;
    --cases)
      CASES_CSV="$2"
      shift 2
      ;;
    --base-port)
      BASE_PORT="$2"
      shift 2
      ;;
    --buffer-pool-size)
      BUFFER_POOL_SIZE="$2"
      shift 2
      ;;
    --db-name)
      DB_NAME="$2"
      shift 2
      ;;
    --tables)
      TABLES="$2"
      shift 2
      ;;
    --table-size)
      TABLE_SIZE="$2"
      TABLE_SIZE_MANUAL=1
      shift 2
      ;;
    --target-data-gib)
      TARGET_DATA_GIB="$2"
      shift 2
      ;;
    --prepare-threads)
      PREPARE_THREADS="$2"
      shift 2
      ;;
    --run-threads)
      RUN_THREADS="$2"
      shift 2
      ;;
    --run-time)
      RUN_TIME="$2"
      shift 2
      ;;
    --report-interval)
      REPORT_INTERVAL="$2"
      shift 2
      ;;
    --skip-prepare)
      DO_PREPARE=0
      shift
      ;;
    --do-cleanup)
      DO_CLEANUP=1
      shift
      ;;
    --skip-cleanup)
      DO_CLEANUP=0
      shift
      ;;
    --help|-h)
      show_help
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $1" >&2
      show_help
      exit 1
      ;;
  esac
done

if [[ -z "$RUN_ROOT" ]]; then
  RUN_ROOT="$BUILD_DIR/numa_sysbench_runs/$(date +%Y%m%d_%H%M%S)"
fi

MYSQLD="$BUILD_DIR/runtime_output_directory/mysqld"
MYSQL="$BUILD_DIR/runtime_output_directory/mysql"
MYSQLADMIN="$BUILD_DIR/runtime_output_directory/mysqladmin"
SYSBENCH_BIN="sysbench"

require_cmd "$SYSBENCH_BIN"
require_cmd awk
require_cmd sed
require_cmd grep
require_cmd wc

if [[ ! -x "$MYSQLD" ]]; then
  echo "ERROR: mysqld not found or not executable: $MYSQLD" >&2
  exit 1
fi
if [[ ! -x "$MYSQL" ]]; then
  echo "ERROR: mysql client not found or not executable: $MYSQL" >&2
  exit 1
fi
if [[ ! -x "$MYSQLADMIN" ]]; then
  echo "ERROR: mysqladmin not found or not executable: $MYSQLADMIN" >&2
  exit 1
fi

if [[ "$CASES_CSV" == *"bind_node"* ]]; then
  require_cmd numactl
fi

mkdir -p "$RUN_ROOT"

IFS=',' read -r -a CASES <<<"$CASES_CSV"

for i in "${!CASES[@]}"; do
  CASES[$i]="$(echo "${CASES[$i]}" | xargs)"
done

if ! [[ "$TABLES" =~ ^[0-9]+$ ]] || [[ "$TABLES" -le 0 ]]; then
  echo "ERROR: --tables must be a positive integer" >&2
  exit 1
fi

if [[ "$TABLE_SIZE_MANUAL" -eq 1 ]]; then
  if ! [[ "$TABLE_SIZE" =~ ^[0-9]+$ ]] || [[ "$TABLE_SIZE" -le 0 ]]; then
    echo "ERROR: --table-size must be a positive integer" >&2
    exit 1
  fi
else
  if ! [[ "$TARGET_DATA_GIB" =~ ^[0-9]+$ ]] || [[ "$TARGET_DATA_GIB" -le 0 ]]; then
    echo "ERROR: --target-data-gib must be a positive integer" >&2
    exit 1
  fi

  target_bytes=$((TARGET_DATA_GIB * 1024 * 1024 * 1024))
  rows_total=$(((target_bytes + ROW_BYTES_EST - 1) / ROW_BYTES_EST))
  TABLE_SIZE=$(((rows_total + TABLES - 1) / TABLES))
fi

EST_CASE_BYTES=$((TABLES * TABLE_SIZE * ROW_BYTES_EST))
EST_ALL_CASES_BYTES=$((EST_CASE_BYTES * ${#CASES[@]}))

if [[ ! -d "$RUN_ROOT" ]]; then
  mkdir -p "$(dirname "$RUN_ROOT")"
fi
SPACE_CHECK_PATH="$RUN_ROOT"
if [[ ! -d "$SPACE_CHECK_PATH" ]]; then
  SPACE_CHECK_PATH="$(dirname "$RUN_ROOT")"
fi
AVAILABLE_BYTES=$(df --output=avail -B1 "$SPACE_CHECK_PATH" | tail -n 1 | tr -d '[:space:]')
RECOMMENDED_BYTES=$((EST_ALL_CASES_BYTES * 12 / 10))

if [[ "$AVAILABLE_BYTES" -lt "$RECOMMENDED_BYTES" ]]; then
  echo "ERROR: insufficient disk space for estimated dataset size." >&2
  echo "  path: $SPACE_CHECK_PATH" >&2
  echo "  available_bytes: $AVAILABLE_BYTES" >&2
  echo "  estimated_data_bytes_all_cases: $EST_ALL_CASES_BYTES" >&2
  echo "  recommended_bytes_with_20pct_headroom: $RECOMMENDED_BYTES" >&2
  echo "Hint: use --run-root on a larger disk (e.g. /home/hdd/...), reduce --cases, or set smaller --target-data-gib." >&2
  exit 1
fi

wait_for_mysql() {
  local socket="$1"
  local retries=60
  for _ in $(seq 1 "$retries"); do
    if "$MYSQL" --no-defaults -uroot -S "$socket" -e "SELECT 1" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

shutdown_mysql() {
  local socket="$1"
  "$MYSQLADMIN" --no-defaults -uroot -S "$socket" shutdown >/dev/null 2>&1 || true
}

summarize_csv() {
  local csv="$1"
  local summary="$2"

  if [[ ! -f "$csv" ]]; then
    echo "CSV file not found: $csv" >"$summary"
    return 0
  fi

  {
    echo "CSV: $csv"
    echo "rows_total: $(wc -l <"$csv")"
    echo
    echo "[query_error_distribution]"
    awk -F, 'NR>1{cnt[$11]++} END{for(k in cnt) print cnt[k],k}' "$csv" | sort -nr
    echo
    echo "[block_state_distribution]"
    awk -F, 'NR>1{cnt[$5]++} END{for(k in cnt) print cnt[k],k}' "$csv" | sort -nr
    echo
    echo "[numa_nodes_for_file_pages_unknown0]"
    awk -F, 'NR>1 && $5=="BUF_BLOCK_FILE_PAGE" && $10==0 {cnt[$8]++}
      END{for(k in cnt) print cnt[k],k}' "$csv" | sort -nr
    echo
    echo "[sample_rows]"
    sed -n '1,8p' "$csv"
  } >"$summary"
}

run_case() {
  local case_name="$1"
  local port="$2"

  local interleave="OFF"
  local bind_node=""

  case "$case_name" in
    baseline)
      interleave="OFF"
      bind_node=""
      ;;
    interleave_on)
      interleave="ON"
      bind_node=""
      ;;
    bind_node0)
      interleave="OFF"
      bind_node="0"
      ;;
    bind_node1)
      interleave="OFF"
      bind_node="1"
      ;;
    *)
      echo "ERROR: unsupported case: $case_name" >&2
      exit 1
      ;;
  esac

  local run_dir="$RUN_ROOT/$case_name"
  local datadir="$run_dir/data"
  local socket="$run_dir/mysql.sock"
  local pid_file="$run_dir/mysql.pid"
  local error_log="$run_dir/error.log"
  local csv="$datadir/innodb_buffer_frame_numa.csv"
  local summary="$run_dir/numa_summary.txt"

  mkdir -p "$run_dir"
  rm -rf "$datadir"
  mkdir -p "$datadir"

  echo "[$case_name] initialize datadir ..."
  "$MYSQLD" \
    --no-defaults \
    --basedir="$BUILD_DIR" \
    --datadir="$datadir" \
    --initialize-insecure \
    >"$run_dir/mysqld_initialize.log" 2>&1

  local -a start_cmd=(
    "$MYSQLD"
    --no-defaults
    --basedir="$BUILD_DIR"
    --datadir="$datadir"
    --socket="$socket"
    --port="$port"
    --pid-file="$pid_file"
    --log-error="$error_log"
    --innodb_buffer_pool_size="$BUFFER_POOL_SIZE"
    --innodb-numa-interleave="$interleave"
    --daemonize
  )

  echo "[$case_name] start mysqld (port=$port, interleave=$interleave, bind_node=${bind_node:-none}) ..."
  if [[ -n "$bind_node" ]]; then
    numactl --cpunodebind="$bind_node" --membind="$bind_node" "${start_cmd[@]}"
  else
    "${start_cmd[@]}"
  fi

  if ! wait_for_mysql "$socket"; then
    echo "ERROR: [$case_name] mysqld did not become ready" >&2
    tail -n 80 "$error_log" || true
    exit 1
  fi

  "$MYSQL" --no-defaults -uroot -S "$socket" \
    -e "CREATE DATABASE IF NOT EXISTS \`$DB_NAME\`;"

  local -a sb_common=(
    "$SYSBENCH_BIN"
    oltp_read_write
    --db-driver=mysql
    --mysql-user=root
    --mysql-socket="$socket"
    --mysql-db="$DB_NAME"
    --mysql-storage-engine=innodb
    --tables="$TABLES"
    --table-size="$TABLE_SIZE"
  )

  if [[ "$DO_PREPARE" -eq 1 ]]; then
    echo "[$case_name] sysbench prepare ..."
    "${sb_common[@]}" --threads="$PREPARE_THREADS" prepare \
      >"$run_dir/sysbench_prepare.log" 2>&1
  fi

  echo "[$case_name] sysbench run (threads=$RUN_THREADS, time=${RUN_TIME}s) ..."
  "${sb_common[@]}" \
    --threads="$RUN_THREADS" \
    --time="$RUN_TIME" \
    --report-interval="$REPORT_INTERVAL" \
    run | tee "$run_dir/sysbench_run.log"

  if [[ "$DO_CLEANUP" -eq 1 ]]; then
    echo "[$case_name] sysbench cleanup ..."
    "${sb_common[@]}" --threads="$PREPARE_THREADS" cleanup \
      >"$run_dir/sysbench_cleanup.log" 2>&1
  fi

  echo "[$case_name] shutdown mysqld (to trigger NUMA CSV dump) ..."
  shutdown_mysql "$socket"

  if [[ ! -f "$csv" ]]; then
    echo "ERROR: [$case_name] expected CSV not found: $csv" >&2
    tail -n 120 "$error_log" || true
    exit 1
  fi

  summarize_csv "$csv" "$summary"

  echo "[$case_name] done."
  echo "  - csv: $csv"
  echo "  - summary: $summary"
}

{
  echo "run_root: $RUN_ROOT"
  echo "build_dir: $BUILD_DIR"
  echo "cases: ${CASES[*]}"
  echo "base_port: $BASE_PORT"
  echo "buffer_pool_size: $BUFFER_POOL_SIZE"
  echo "db_name: $DB_NAME"
  echo "tables: $TABLES"
  echo "table_size: $TABLE_SIZE"
  if [[ "$TABLE_SIZE_MANUAL" -eq 1 ]]; then
    echo "table_size_mode: manual"
  else
    echo "table_size_mode: auto_from_target_data_gib"
    echo "target_data_gib_per_case: $TARGET_DATA_GIB"
    echo "row_bytes_estimate: $ROW_BYTES_EST"
  fi
  echo "estimated_data_gib_per_case: $(awk -v b="$EST_CASE_BYTES" 'BEGIN{printf \"%.2f\", b/1024/1024/1024}')"
  echo "estimated_data_gib_all_cases: $(awk -v b="$EST_ALL_CASES_BYTES" 'BEGIN{printf \"%.2f\", b/1024/1024/1024}')"
  echo "disk_space_check_path: $SPACE_CHECK_PATH"
  echo "disk_available_gib: $(awk -v b="$AVAILABLE_BYTES" 'BEGIN{printf \"%.2f\", b/1024/1024/1024}')"
  echo "prepare_threads: $PREPARE_THREADS"
  echo "run_threads: $RUN_THREADS"
  echo "run_time: $RUN_TIME"
  echo "report_interval: $REPORT_INTERVAL"
  echo "skip_prepare: $((1 - DO_PREPARE))"
  echo "do_cleanup: $DO_CLEANUP"
} | tee "$RUN_ROOT/experiment_config.txt"

port="$BASE_PORT"
for case_name in "${CASES[@]}"; do
  run_case "$case_name" "$port"
  port=$((port + 1))
done

echo
echo "All cases completed. Output root: $RUN_ROOT"
