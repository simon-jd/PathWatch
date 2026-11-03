延迟为五轮分位数的中位数（不是合并所有样本的分位数），单位ms；H2保留量化上下界。CPU为五轮总CPU秒/总完成数，单位us/请求。错误各类别可能重叠，不能直接相加。

|协议|RPS|代理|实际RPS|p50 ms|p99 ms|p99.9 ms|CPU us/完成|错误类别|期末未完成|
|---|---:|---|---:|---:|---:|---:|---:|---|---:|
|h1|1000|pathwatch|996.59|1.410|4.950|7.220|177.75|http=0, connect=0, read=0, write=0, timeout=0|136|
|h1|1000|envoy|997.79|1.560|3.500|4.900|315.06|http=0, connect=0, read=0, write=0, timeout=0|82|
|h1|1000|nginx|998.62|1.120|3.100|4.370|152.97|http=0, connect=0, read=0, write=0, timeout=0|15|
|h1|5000|pathwatch|4985.93|1.190|3.690|5.640|120.11|http=0, connect=0, read=0, write=0, timeout=0|46|
|h1|5000|envoy|4983.90|1.740|6.350|8.510|145.13|http=0, connect=0, read=0, write=0, timeout=0|103|
|h1|5000|nginx|4987.90|1.210|3.590|5.650|114.05|http=0, connect=0, read=0, write=0, timeout=0|61|
|h1|10000|pathwatch|9967.62|1.160|3.270|4.640|72.57|http=0, connect=0, read=0, write=0, timeout=0|139|
|h1|10000|envoy|9971.56|1.600|4.060|7.610|86.68|http=0, connect=0, read=0, write=0, timeout=0|101|
|h1|10000|nginx|9963.65|1.340|3.790|5.220|66.15|http=0, connect=0, read=0, write=0, timeout=0|108|
|h1|20000|pathwatch|19903.47|1.410|3.090|5.080|44.17|http=0, connect=0, read=0, write=0, timeout=0|176|
|h1|20000|envoy|19926.85|3.030|7.410|18.030|71.87|http=0, connect=0, read=0, write=0, timeout=0|250|
|h1|20000|nginx|19950.67|1.470|3.260|5.020|41.09|http=0, connect=0, read=0, write=0, timeout=0|166|
|h2|1000|pathwatch|999.95|1.867|10.720–10.727|14.640–14.647|156.62|failed=0, errored=0, timeout=0|0|
|h2|1000|envoy|999.95|7.060–7.063|18.544–18.559|21.280–21.295|235.29|failed=0, errored=0, timeout=0|0|
|h2|1000|nginx|999.97|0.710|5.292–5.295|8.880–8.887|149.97|failed=0, errored=0, timeout=0|0|
|h2|5000|pathwatch|4999.31|5.040–5.043|21.712–21.727|23.120–23.135|125.87|failed=0, errored=0, timeout=0|310|
|h2|5000|envoy|4999.67|2.656–2.657|8.912–8.919|13.552–13.559|151.03|failed=0, errored=0, timeout=0|0|
|h2|5000|nginx|4999.73|0.320|3.348–3.349|5.556–5.559|116.25|failed=0, errored=0, timeout=0|0|
|h2|10000|pathwatch|9998.90|10.168–10.175|11.600–11.607|13.544–13.551|73.93|failed=0, errored=0, timeout=0|472|
|h2|10000|envoy|9999.76|0.966|3.734–3.735|7.072–7.075|90.33|failed=0, errored=0, timeout=0|63|
|h2|10000|nginx|9999.76|0.187|2.116–2.117|4.920–4.923|69.24|failed=0, errored=0, timeout=0|33|
|h2|20000|pathwatch|19996.86|10.376–10.383|11.344–11.351|14.096–14.103|44.60|failed=0, errored=0, timeout=0|1050|
|h2|20000|envoy|19998.43|2.240–2.241|37.472–37.503|111.104–111.167|74.29|failed=0, errored=0, timeout=0|398|
|h2|20000|nginx|19998.57|0.193|2.466–2.467|5.940–5.943|43.68|failed=0, errored=0, timeout=0|64|
