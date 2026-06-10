# Natural-Language Query Catalog

Ask these in plain English. The agent (with the DocumentDB Agent Kit) maps each to a
**MCP tool** and a **MongoDB query/aggregation**, runs it against `traveldb`, and explains
the result. The MongoDB shown below is what the agent generates for you — you never write it.

> Schema: `reservations` join `customers` (via `customer_id`), `destinations`
> (`destination_id` / `destination_city`), `flights` (`flight_id`). Dates are stored as
> `YYYY-MM-DD` strings, so range filters use ordinary string comparison.

---

## Browsing & lookups → `find_documents` / `sample_documents`

**1. "Show me 5 sample reservations."**
`sample_documents` on `reservations` (size 5).

**2. "List all confirmed reservations to Tokyo, newest check-in first."**
```js
// find_documents
db.reservations.find(
  { status: "confirmed", destination_city: "Tokyo" }
).sort({ check_in: -1 })
```

**3. "Which destinations in Asia are city-breaks rated above 4.5?"**
```js
db.destinations.find(
  { region: "Asia", category: "city-break", rating: { $gt: 4.5 } },
  { city: 1, country: 1, rating: 1 }
)
```

**4. "Find customers in the platinum or gold loyalty tier."**
```js
db.customers.find({ loyalty_tier: { $in: ["platinum", "gold"] } })
```

---

## Counts & quick metrics → `count_documents`

**5. "How many reservations are confirmed vs cancelled?"**
```js
db.reservations.countDocuments({ status: "confirmed" })
db.reservations.countDocuments({ status: "cancelled" })
```

**6. "How many trips check in during summer 2026 (Jun–Aug)?"**
```js
db.reservations.countDocuments({
  check_in: { $gte: "2026-06-01", $lte: "2026-08-31" }
})
```

---

## Aggregations & analytics → `aggregate`

**7. "What's our total booked revenue (confirmed + completed)?"**
```js
db.reservations.aggregate([
  { $match: { status: { $in: ["confirmed", "completed"] } } },
  { $group: { _id: null, revenue: { $sum: "$total_price" }, trips: { $sum: 1 } } }
])
```

**8. "Top 3 destination cities by total revenue."**
```js
db.reservations.aggregate([
  { $match: { status: { $in: ["confirmed", "completed"] } } },
  { $group: { _id: "$destination_city", revenue: { $sum: "$total_price" } } },
  { $sort: { revenue: -1 } },
  { $limit: 3 }
])
```

**9. "Average nights stayed per region."** (joins reservations → destinations)
```js
db.reservations.aggregate([
  { $lookup: { from: "destinations", localField: "destination_id",
               foreignField: "_id", as: "dest" } },
  { $unwind: "$dest" },
  { $group: { _id: "$dest.region", avgNights: { $avg: "$nights" } } },
  { $sort: { avgNights: -1 } }
])
```

**10. "Who are my top 5 customers by total spend?"**
```js
db.reservations.aggregate([
  { $match: { status: { $in: ["confirmed", "completed"] } } },
  { $group: { _id: "$customer_id", name: { $first: "$customer_name" },
              spend: { $sum: "$total_price" }, trips: { $sum: 1 } } },
  { $sort: { spend: -1 } },
  { $limit: 5 }
])
```

**11. "Revenue by month for 2026."**
```js
db.reservations.aggregate([
  { $match: { status: { $in: ["confirmed", "completed"] } } },
  { $group: { _id: { $substr: ["$check_in", 0, 7] },   // "2026-03"
              revenue: { $sum: "$total_price" } } },
  { $sort: { _id: 1 } }
])
```

**12. "Average flight fare by airline."**
```js
db.flights.aggregate([
  { $group: { _id: "$airline", avgFare: { $avg: "$base_fare" } } },
  { $sort: { avgFare: -1 } }
])
```

**13. "Which payment methods are most used, and how much revenue each?"**
```js
db.reservations.aggregate([
  { $match: { "payment.paid": true } },
  { $group: { _id: "$payment.method", revenue: { $sum: "$total_price" },
              count: { $sum: 1 } } },
  { $sort: { revenue: -1 } }
])
```

---

## Index & performance tuning → `optimize_find_query` / `explain_aggregate_query` / `create_index`

**14. "This is slow: reservations filtered by status and check-in date. How should I index it?"**
The agent runs `optimize_find_query` / `explain_aggregate_query`, sees a COLLSCAN, and
(following the kit's **indexing** skill — ESR rule) suggests:
```js
db.reservations.createIndex({ status: 1, check_in: 1 })   // create_index
```

**15. "Show the indexes on the reservations collection and their sizes."**
`list_indexes` + `index_stats` on `reservations`.

---

## Going further (kit skills, same DB)

- **Full-text search** — *"Add a BM25 search index on destination tags and find 'beach nightlife' matches."* → kit's **full-text-search** skill (`createSearchIndexes` + `$search`).
- **Vector search** — *"I want semantic search over destination descriptions."* → **vector-search** skill (`cosmosSearch` with DiskANN/HNSW).
- **Data modeling** — *"Should reservations embed the customer or reference it?"* → **data-modeling** skill (embed-vs-reference, 16 MB limit).
- **Connection tuning** — *"How do I set the pool size and retry policy for a serverless app?"* → **connection** skill.

---

### Tips for good results
- Name the collection if the agent guesses wrong: *"…in the **reservations** collection."*
- Ask for the query: *"…and show me the MongoDB aggregation you used."*
- Chain: *"Now break that revenue down by loyalty tier."* — the agent keeps context.
