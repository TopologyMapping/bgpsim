# bgpsim

BGP path propagation inference

## Running the tests

We have some tests to check the propagation algorithm in pre-built topologies.  Run with:

```bash
python3 -m unittest tests/test_bgpsim.py
```

## Basic benchmarking

You can check the runtime of random path inferences on CAIDA's January 2020 graph by using the `tests/bench_bgpsim.py` script. It reports 5 averages over 32 full inference runs each. Disabling assertions with `-O` makes the code significantly faster as it is pretty heavy on asserts.

```bash
$ python3 tests/bench_bgpsim.py
[1065.9921099510975, 1129.7197931839619, 1341.9510222299723, 1212.2649150219513, 1117.318360270001]
$ python3 -O tests/bench_bgpsim.py
[244.108187089907, 246.5742465169169, 244.00426896300633, 235.13479439693037, 255.45060764998198]
```

## References

You man want to check these papers on an [introduction to BGP routing policies][bgp-policies], and on [how policies can be inferred in the wild][caida-asrel].

## TO-DO

* Write tests for poisoned announcements. The code should work for announcements with poisoning, but there are no tests for this functionality yet.

[bgp-policies]: https://doi.org/10.1109/MNET.2005.1541715
[caida-asrel]: https://doi.org/10.1145/2504730.2504735
