"""Tile endpoint: parameter validation, ETag / 304 and byte determinism."""
import mapbox_vector_tile



def test_tile_content_type_and_headers(client, publish):
    ds = publish()["id"]
    resp = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/vnd.mapbox-vector-tile"
    assert resp.headers["etag"].startswith('"')
    assert resp.headers["etag"].endswith('"')
    assert len(resp.headers["etag"]) == 66  # " + 64 hex + "
    assert resp.headers["x-revision"] == "1"
    assert "if-none-match" in resp.headers["vary"].lower()


def test_repeat_request_identical_bytes_and_etag(client, publish):
    ds = publish()["id"]
    r1 = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt")
    r2 = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt")
    assert r1.content == r2.content
    assert r1.headers["etag"] == r2.headers["etag"]


def test_if_none_match_returns_304(client, publish):
    ds = publish()["id"]
    r1 = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt")
    r2 = client.get(
        f"/datasets/{ds}/tiles/0/0/0.mvt",
        headers={"If-None-Match": r1.headers["etag"]},
    )
    assert r2.status_code == 304
    assert r2.content == b""
    assert r2.headers["etag"] == r1.headers["etag"]


def test_if_none_match_other_etag_returns_200(client, publish):
    ds = publish()["id"]
    resp = client.get(
        f"/datasets/{ds}/tiles/0/0/0.mvt",
        headers={"If-None-Match": '"00"*32"'},
    )
    assert resp.status_code == 200


def test_if_none_match_star(client, publish):
    ds = publish()["id"]
    resp = client.get(
        f"/datasets/{ds}/tiles/0/0/0.mvt", headers={"If-None-Match": "*"}
    )
    assert resp.status_code == 304


def test_head_tile_request(client, publish):
    ds = publish()["id"]
    get = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt")
    head = client.head(f"/datasets/{ds}/tiles/0/0/0.mvt")
    assert head.status_code == 200
    assert head.headers["etag"] == get.headers["etag"]
    assert head.content == b""


def test_tile_dataset_404(client):
    assert client.get("/datasets/nope/tiles/0/0/0.mvt").status_code == 404


def test_tile_unknown_revision_404(client, publish):
    ds = publish()["id"]
    resp = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt?revision=99")
    assert resp.status_code == 404


def test_pinned_revision_header_is_immutable(client, publish):
    ds = publish()["id"]
    resp = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt?revision=1")
    assert resp.status_code == 200
    assert "immutable" in resp.headers["cache-control"]


def test_head_revision_is_not_cache_immutable(client, publish):
    ds = publish()["id"]
    resp = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt")
    assert "immutable" not in resp.headers["cache-control"]


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------


def test_invalid_tile_parameters(client, publish):
    ds = publish()["id"]
    for path, _label in [
        ("15/0/0.mvt", "zoom above 14"),
        ("-1/0/0.mvt", "negative zoom"),
        ("2/4/0.mvt", "x out of range"),
        ("2/0/4.mvt", "y out of range"),
        ("2/-1/0.mvt", "negative x"),
        ("abc/0/0.mvt", "non-integer z"),
        ("0/0/-1.mvt", "negative y"),
    ]:
        resp = client.get(f"/datasets/{ds}/tiles/{path}")
        assert resp.status_code == 400, path


# ---------------------------------------------------------------------------
# Tile content
# ---------------------------------------------------------------------------


def test_tile_decodes_as_mvt_version_2(client, publish):
    ds = publish()["id"]
    resp = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt")
    decoded = mapbox_vector_tile.decode(
        resp.content, default_options={"y_coord_down": True}
    )
    assert "demo" in decoded
    layer = decoded["demo"]
    assert layer["version"] == 2
    assert layer["extent"] == 4096
    assert len(layer["features"]) == 4


def test_empty_tile_is_valid_and_stable(client):
    body = (
        b'{"type":"FeatureCollection","features":['
        b'{"type":"Feature","id":1,"properties":{"a":1},'
        b'"geometry":{"type":"Point","coordinates":[45,45]}}]}'
    )
    ds = client.post(
        "/datasets", content=body, headers={"content-type": "application/json"}
    ).json()["id"]
    # (45,45) lands in tile (1,1,0) at z=1; far away tile (0,0,1) is empty.
    empty = client.get(f"/datasets/{ds}/tiles/1/0/1.mvt")
    assert empty.status_code == 200
    again = client.get(f"/datasets/{ds}/tiles/1/0/1.mvt")
    assert empty.content == again.content
    assert empty.headers["etag"] == again.headers["etag"]
    decoded = mapbox_vector_tile.decode(
        empty.content, default_options={"y_coord_down": True}
    )
    assert decoded["default"]["features"] == []

    hit = client.get(f"/datasets/{ds}/tiles/1/1/0.mvt")
    decoded_hit = mapbox_vector_tile.decode(
        hit.content, default_options={"y_coord_down": True}
    )
    assert len(decoded_hit["default"]["features"]) == 1


def test_feature_order_is_deterministic_regardless_of_input_order(client):
    template = (
        b'{"type":"FeatureCollection","features":[%s]}'
    )
    feature = (
        b'{"type":"Feature","id":%d,"properties":{"k":"v%02d"},'
        b'"geometry":{"type":"Point","coordinates":[0,0]}}'
    )
    ids = [3, 1, 2, 10, 7]
    order_a = b",".join(feature % (i, i) for i in ids)
    order_b = b",".join(feature % (i, i) for i in reversed(ids))

    ds_a = client.post(
        "/datasets?layer=L", content=template % order_a
    ).json()["id"]
    ds_b = client.post(
        "/datasets?layer=L", content=template % order_b
    ).json()["id"]
    tile_a = client.get(f"/datasets/{ds_a}/tiles/1/1/0.mvt").content
    tile_b = client.get(f"/datasets/{ds_b}/tiles/1/1/0.mvt").content
    assert tile_a == tile_b

    decoded = mapbox_vector_tile.decode(
        tile_a, default_options={"y_coord_down": True}
    )
    assert [f["id"] for f in decoded["L"]["features"]] == sorted(ids)


# ---------------------------------------------------------------------------
# Regression: valid sub-grid polygon collapses to <= 2 points when quantised.
# Previously GEOSException inside remove_repeated_points surfaced as HTTP 500.
# ---------------------------------------------------------------------------


# 0.005-degree box around (0, 0): smaller than one 4096-grid unit at z=0.
TINY_POLYGON = (
    b'{"type":"Polygon","coordinates":[[[-0.0025,-0.0025],[0.0025,-0.0025],'
    b'[0.0025,0.0025],[-0.0025,0.0025],[-0.0025,-0.0025]]]}'
)
BIG_POLYGON = (
    b'{"type":"Polygon","coordinates":[[[-10,-10],[10,-10],[10,10],'
    b'[-10,10],[-10,-10]]]}'
)


def test_collapsed_polygon_tile_returns_200_encodable_mvt(client):
    body = (
        b'{"type":"FeatureCollection","features":['
        b'{"type":"Feature","id":1,"properties":{"kind":"tiny"},'
        b'"geometry":' + TINY_POLYGON + b'},'
        b'{"type":"Feature","id":2,"properties":{"kind":"big"},'
        b'"geometry":' + BIG_POLYGON + b'},'
        b'{"type":"Feature","id":3,"properties":{},'
        b'"geometry":{"type":"Point","coordinates":[0,0]}}]}'
    )
    ds = client.post(
        "/datasets?layer=L",
        content=body,
        headers={"content-type": "application/geo+json"},
    ).json()["id"]

    resp = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/vnd.mapbox-vector-tile"
    decoded = mapbox_vector_tile.decode(
        resp.content, default_options={"y_coord_down": True}
    )
    # The collapsed polygon is dropped; the other features are not cleared.
    features = decoded["L"]["features"]
    assert sorted(f["geometry"]["type"] for f in features) == [
        "Point",
        "Polygon",
    ]
    assert [
        f["properties"].get("kind")
        for f in features
        if f["geometry"]["type"] == "Polygon"
    ] == ["big"]


def test_tile_with_only_collapsed_polygon_is_empty_but_200(client):
    body = (
        b'{"type":"FeatureCollection","features":['
        b'{"type":"Feature","id":1,"properties":{},"geometry":'
        + TINY_POLYGON
        + b'}]}'
    )
    ds = client.post(
        "/datasets?layer=L",
        content=body,
        headers={"content-type": "application/geo+json"},
    ).json()["id"]
    resp = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt")
    assert resp.status_code == 200
    decoded = mapbox_vector_tile.decode(
        resp.content, default_options={"y_coord_down": True}
    )
    assert decoded["L"]["features"] == []


def test_collapsed_polygon_tile_bytes_and_pinned_etag_are_stable(client):
    body = (
        b'{"type":"FeatureCollection","features":['
        b'{"type":"Feature","id":1,"properties":{},"geometry":'
        + TINY_POLYGON
        + b'},{"type":"Feature","id":2,"properties":{},"geometry":'
        + BIG_POLYGON
        + b'}]}'
    )
    ds = client.post(
        "/datasets?layer=L",
        content=body,
        headers={"content-type": "application/geo+json"},
    ).json()["id"]
    head = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt")
    pinned = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt?revision=1")
    again = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt")
    assert head.status_code == pinned.status_code == 200
    assert head.content == pinned.content == again.content
    assert head.headers["etag"] == pinned.headers["etag"] == again.headers["etag"]

