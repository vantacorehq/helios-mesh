"""Demo: publish an object, lose nodes, recover, energy-aware route."""
from helios import Node, publish, fetch, Router, autonomy_days, daily_harvest_wh, latitude_from_polaris

nodes = [Node(f"DC{i}") for i in range(6)]
data = b"Helios-Mesh: knowledge archive " * 50
c, n = publish(data, nodes)
print("CID:", c[:20] + "…", "size:", n)

for dead in nodes[:2]:
    dead.shards.clear()
print("Lost 2 of 6 nodes (~4 of 12 shards) ->", fetch(c, n, nodes) == data and "data recovered")

r = Router()
for name, soc in [("A", .9), ("B", .1), ("C", .7), ("D", .8), ("E", .95)]:
    r.add_node(name, soc)
for u, v, etx, ch in [("A", "B", 1.1, "lora"), ("B", "E", 1.2, "lora"), ("A", "C", 1.5, "lora"),
                      ("C", "D", 1.4, "lora"), ("D", "E", 1.3, "uhf"), ("A", "E", 3.0, "hf")]:
    r.add_link(u, v, etx, ch)
path, hops, cost = r.route("A", "E")
print("Route:", " → ".join(path), hops, f"cost {cost:.2f}")

print(f"400 W panel yields {daily_harvest_wh(400, 4):.0f} Wh/day; 2 kWh battery at 540 Wh/day lasts {autonomy_days(2000, 540):.1f} days")
print(f"Latitude from Polaris: {latitude_from_polaris(55.2, 40):.2f} deg")
