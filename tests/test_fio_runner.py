import pytest
from app.core.fio_runner import FioRunner

def test_parse_fio_text_output():
    sample_output = """
    seq-read: (g=0): rw=read, bs=(R) 1024KiB-1024KiB, (W) 1024KiB-1024KiB, (T) 1024KiB-1024KiB, ioengine=psync, iodepth=32
    Starting 1 process
    Jobs: 1 (f=1): [R(1)][100.0%][r=512MiB/s,w=0KiB/s][r=512,w=0 IOPS][eta 00m:00s]
    seq-read: (groupid=0, jobs=1): err= 0: pid=12345: Fri Aug 22 12:00:00 2026
      read: IOPS=512, BW=512MiB/s (537MB/s)(30.0GiB/60001msec)
        clat (usec): min=1800, max=4500, avg=1953.12, stdev=210.45
      lat (usec): min=1810, max=4520, avg=1960.00, stdev=211.00
      cpu          : usr=1.20%, sys=14.50%, ctx=30720, majf=0, minf=28
      IO depths    : 1=0.1%, 2=0.1%, 4=0.1%, 8=0.1%, 16=0.1%, 32=99.6%, >=64=0.0%
    """

    fio = FioRunner()
    metrics = fio.parse_fio_output("seq-read", sample_output, 60.0)

    assert metrics.job_name == "seq-read"
    assert metrics.passed is True
    assert metrics.total_errors == 0
    assert metrics.read_bw_mb_s >= 500.0


def test_parse_fio_json_output():
    sample_json = """
    {
      "fio version": "fio-3.33",
      "jobs": [
        {
          "jobname": "seq-write",
          "groupid": 0,
          "error": 0,
          "read": { "io_bytes": 0, "bw_bytes": 0, "iops": 0 },
          "write": {
            "io_bytes": 31457280000,
            "bw_bytes": 524288000,
            "iops": 500.0,
            "clat_ns": {
              "min": 1000000,
              "max": 5000000,
              "mean": 1950000.0,
              "percentile": {
                "99.000000": 3200000
              }
            }
          }
        }
      ]
    }
    """

    fio = FioRunner()
    metrics = fio.parse_fio_output("seq-write", sample_json, 60.0)

    assert metrics.job_name == "seq-write"
    assert metrics.passed is True
    assert metrics.total_errors == 0
    assert metrics.write_bw_mb_s == 500.0
    assert metrics.write_iops == 500.0
    assert metrics.write_lat_mean_ms == 1.95
    assert metrics.write_lat_p99_ms == 3.2
