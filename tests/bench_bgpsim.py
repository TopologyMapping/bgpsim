import os
import random
import sys
import timeit
import urllib

sys.path.append(os.getcwd())
from bgpsim import (
    Announcement,
    ASGraph,
)

CAIDA_AS_RELATIONSHIPS_URL = (
    "http://data.caida.org/datasets/as-relationships/serial-1/20200101.as-rel.txt.bz2"
)


def random_inference(graph):
    sources = random.sample(graph.g.nodes, 2)
    announce = Announcement.make_anycast_announcement(graph, sources)
    g1 = graph.clone()
    g1.infer_paths(announce)


def bench():
    url = urllib.parse.urlparse(CAIDA_AS_RELATIONSHIPS_URL)
    db_filename = os.path.basename(url.path)
    db_filepath = os.path.join("tests", db_filename)
    if not os.path.exists(db_filepath):
        urllib.request.urlretrieve(CAIDA_AS_RELATIONSHIPS_URL, db_filepath)
    graph = ASGraph.read_caida_asrel_graph(db_filepath)

    t = timeit.Timer(lambda: random_inference(graph))
    print(t.repeat(repeat=5, number=32))


if __name__ == "__main__":
    bench()
