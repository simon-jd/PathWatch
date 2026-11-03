延迟为五轮分位数的中位数（不是合并所有样本的分位数），单位ms；H2保留量化上下界。CPU为五轮总CPU秒/总完成数，单位us/请求。错误各类别可能重叠，不能直接相加。

|协议|RPS|代理|实际RPS|p50 ms|p99 ms|p99.9 ms|CPU us/完成|错误类别|期末未完成|
|---|---:|---|---:|---:|---:|---:|---:|---|---:|
|h1|1000|pathwatch|997.00|0.940|2.590|4.630|64.80|http=0, connect=0, read=0, write=0, timeout=0|91|
|h1|1000|envoy|997.41|1.140|3.340|6.080|117.21|http=0, connect=0, read=0, write=0, timeout=0|47|
|h1|1000|nginx|997.40|0.900|2.230|4.030|49.58|http=0, connect=0, read=0, write=0, timeout=0|49|
|h1|5000|pathwatch|4987.92|0.990|2.960|7.200|46.53|http=0, connect=0, read=0, write=0, timeout=0|107|
|h1|5000|envoy|4983.92|1.410|5.740|21.100|89.26|http=0, connect=0, read=0, write=0, timeout=0|125|
|h1|5000|nginx|4979.97|1.040|2.700|7.680|38.58|http=0, connect=0, read=0, write=0, timeout=0|147|
|h1|10000|pathwatch|9963.51|1.070|3.170|13.070|40.22|http=0, connect=0, read=0, write=0, timeout=0|200|
|h1|10000|envoy|9959.62|1.750|10.020|60.420|80.04|http=0, connect=0, read=0, write=0, timeout=0|250|
|h1|10000|nginx|9967.64|0.980|3.190|6.100|32.82|http=0, connect=0, read=0, write=0, timeout=0|162|
|h1|20000|pathwatch|19950.60|1.350|5.140|33.820|36.77|http=0, connect=0, read=0, write=0, timeout=0|262|
|h1|20000|envoy|19889.32|3.420|948.220|1440.000|74.80|http=0, connect=0, read=0, write=0, timeout=0|302|
|h1|20000|nginx|19919.06|1.160|7.260|49.500|29.84|http=0, connect=0, read=0, write=0, timeout=0|186|
