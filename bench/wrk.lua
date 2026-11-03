-- Preserve DNS SNI/Host while forcing a literal, owned loopback destination.
function wrk.resolve(host, service)
  assert(host == "bench.pathwatch.test" and service == "18443")
  wrk.addrs = wrk.lookup("127.0.0.1", service)
end
-- Native summary duration is microseconds; the human line rounds 120s to2.00m.
function done(summary, latency, requests)
  io.write(string.format("BENCH_WRK_DURATION_US %.0f\n", summary.duration))
end
