# elder_ui.py
import requests
import streamlit as st
import pandas as pd
from streamlit.components.v1 import html as st_html
import pydeck as pdk
from urllib.parse import urlparse, parse_qs, quote_plus  # ← added

st.set_page_config(page_title="Finding Dory — Simple Console", layout="centered")

# ------------------------
# Route helpers
# ------------------------
def _decode_polyline(s: str):
    """Decode Google-style encoded polyline → [[lat, lon], ...]."""
    coords, index, lat, lng = [], 0, 0, 0
    while index < len(s):
        shift = result = 0
        while True:
            b = ord(s[index]) - 63; index += 1
            result |= (b & 0x1f) << shift; shift += 5
            if b < 0x20: break
        dlat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += dlat
        shift = result = 0
        while True:
            b = ord(s[index]) - 63; index += 1
            result |= (b & 0x1f) << shift; shift += 5
            if b < 0x20: break
        dlng = ~(result >> 1) if (result & 1) else (result >> 1)
        lng += dlng
        coords.append([lat / 1e5, lng / 1e5])
    return coords

def _normalize_route_coords(raw):
    """
    Accepts:
      - encoded polyline string
      - {"coordinates": [[lon,lat], ...]}
      - [[lat,lon], ...] or [[lon,lat], ...]
    Returns: [[lon,lat], ...] for pydeck PathLayer.
    """
    pts = []
    if not raw:
        return pts

    if isinstance(raw, str):
        pts = _decode_polyline(raw)  # [[lat,lon]]
    elif isinstance(raw, dict) and isinstance(raw.get("coordinates"), list):
        pts = raw["coordinates"]     # [[lon,lat]]
    else:
        pts = raw                    # assume list

    if not pts:
        return []

    # If it looks like [lat,lon], flip to [lon,lat]
    a, b = pts[0][0], pts[0][1]
    if abs(a) <= 90 and abs(b) <= 180:
        return [[p[1], p[0]] for p in pts]
    return pts  # already [lon,lat]

# ------------------------
# WhatsApp link helper
# ------------------------
def _wa_append_maps(wa_url: str, *, maps_url: str | None = None, lat: float | None = None, lng: float | None = None) -> str:
    """
    Take a wa.me link and append a maps URL to its text param.
    If maps_url not provided, builds https://www.google.com/maps?q=lat,lng when lat/lng are given.
    """
    if not wa_url:
        return wa_url
    if not maps_url and lat is not None and lng is not None:
        maps_url = f"https://www.google.com/maps?q={lat:.4f},{lng:.4f}"
    if not maps_url:
        return wa_url

    try:
        u = urlparse(wa_url)
        phone = u.path.strip("/").split("/")[-1]
        qs = parse_qs(u.query)
        text = (qs.get("text", ["EMERGENCY: I need help."])[0]).strip()
        if maps_url not in text:
            text = f"{text} {maps_url}"
        return f"https://wa.me/{phone}?text={quote_plus(text)}"
    except Exception:
        # Fallback: best-effort append
        return f"{wa_url.split('?')[0]}?text={quote_plus('EMERGENCY: I need help. ' + maps_url)}"

# ------------------------
# Accessible styling
# ------------------------
st.markdown("""
<style>
html, body, [class*="css"] { font-size: 18px !important; }
.block-container { padding-top: 2rem; padding-bottom: 3rem; }
.bigbtn button { padding: 0.9rem 1.2rem; font-size: 1.1rem; border-radius: 14px; }
.card { padding: 1rem 1.2rem; border: 1px solid rgba(120,120,120,.35);
        border-radius: 16px; background: rgba(255,255,255,.03); margin-bottom: .8rem; }
.small { opacity:.8; font-size: 0.95rem; }
hr { margin: 0.75rem 0 1rem 0; }
</style>
""", unsafe_allow_html=True)

# ------------------------
# Sidebar
# ------------------------
with st.sidebar:
    st.header("Finding Dory")
    base_url = st.text_input("Backend URL", "http://127.0.0.1:8000", key="base_url")
    user_id = st.number_input("User ID", min_value=1, value=1, step=1, key="uid")
    agent_timeout = st.slider("Agent timeout (seconds)", 10, 300, 120, 5, key="agent_to")
    if st.button("Ping backend", key="ping"):
        try:
            r = requests.get(f"{base_url}/", timeout=5)
            r.raise_for_status()
            st.success("Backend reachable ✅")
        except Exception as e:
            st.error(f"Backend not reachable: {e}")

# ------------------------
# HTTP helpers
# ------------------------
def api_call(method, path, *, params=None, body=None, timeout=15):
    url = f"{base_url}{path}"
    try:
        if method == "GET":
            resp = requests.get(url, params=params, timeout=timeout)
        else:
            resp = requests.post(url, params=params, json=body, timeout=timeout)
        ctype = (resp.headers.get("content-type") or "").lower()
        data = resp.json() if "application/json" in ctype else resp.text
        return resp.ok, data
    except Exception as e:
        return False, {"error": str(e)}

def show_error_block(label, payload):
    st.markdown(
        f"<div class='card'><b>{label}</b><br><span class='small'>{payload}</span></div>",
        unsafe_allow_html=True
    )

# ------------------------
# Session state for route overlays (must exist before UI renders)
# ------------------------
if "route_coords" not in st.session_state:
    st.session_state["route_coords"] = None  # [[lon,lat], ...]
if "route_to_name" not in st.session_state:
    st.session_state["route_to_name"] = None
if "route_dest_point" not in st.session_state:
    st.session_state["route_dest_point"] = None  # {"lon":..., "lat":...}
if "route_steps" not in st.session_state:
    st.session_state["route_steps"] = []

# ------------------------
# Map renderer (uses session route overlay when present)
# ------------------------
def render_map(lat: float, lng: float):
    layers = []

    # User location
    layers.append(pdk.Layer(
        "ScatterplotLayer",
        data=[{"lon": float(lng), "lat": float(lat)}],
        get_position="[lon, lat]",
        get_radius=80,
        pickable=True,
        opacity=0.9,
    ))

    # Destination point (if any)
    dest = st.session_state.get("route_dest_point")
    if dest:
        layers.append(pdk.Layer(
            "ScatterplotLayer",
            data=[{"lon": float(dest["lon"]), "lat": float(dest["lat"])}],
            get_position="[lon, lat]",
            get_radius=100,
            opacity=0.9,
        ))

    # Route path (if any)
    coords = st.session_state.get("route_coords")
    if coords:
        layers.append(pdk.Layer(
            "PathLayer",
            data=[{"path": coords}],
            get_path="path",
            width_scale=1,
            width_min_pixels=4,
            pickable=False,
        ))

    view_state = pdk.ViewState(latitude=float(lat), longitude=float(lng), zoom=13, pitch=0)
    deck = pdk.Deck(layers=layers, initial_view_state=view_state, map_provider="carto")
    st.pydeck_chart(deck, use_container_width=True)

# ------------------------
# Tabs
# ------------------------
tab_dash, tab_loc, tab_agent = st.tabs(["🏠 Dashboard", "📍 Location Ping", "💬 Agent Chat"])

# ===================== Dashboard =====================
with tab_dash:
    st.subheader("Today at a glance")

    # Notifications
    ok, notifs = api_call("GET", f"/api/notifications/{int(user_id)}", timeout=8)
    if ok and isinstance(notifs, list) and notifs:
        st.markdown("**Notifications**")
        for n in notifs[:6]:
            with st.container():
                st.markdown(
                    f"<div class='card'><b>{n.get('title','')}</b> "
                    f"<span class='small'>— {n.get('time','')}</span><br>{n.get('body','')}</div>",
                    unsafe_allow_html=True
                )
                meta = (n or {}).get("metadata") or {}
                if meta.get("type") == "medication":
                    med_name = meta.get("med_name")
                    colA, colB = st.columns([1, 3])
                    with colA:
                        if st.button(f"Taken: {med_name}", key=f"take-{n.get('id')}"):
                            ok2, res = api_call(
                                "POST",
                                f"/api/medications/{int(user_id)}/taken",
                                params={"med_name": med_name},
                                timeout=10
                            )
                            if ok2:
                                st.success("Marked taken")
                                ok, notifs = api_call("GET", f"/api/notifications/{int(user_id)}", timeout=8)
                            else:
                                show_error_block("Take med failed", res)
    elif ok:
        st.info("No notifications yet.")
    else:
        show_error_block("Notifications error", notifs)

    st.divider()

    # Primary contact + deep links
    st.markdown("**Primary Contact**")
    ok, primary = api_call("GET", "/api/contacts/primary",
                           params={"user_id": int(user_id)}, timeout=6)
    if ok and (primary or {}).get("contact"):
        c = primary["contact"]
        st.markdown(
            f"<div class='card'>"
            f"<b>{c.get('name','(unknown)')}</b> — {c.get('relation','') or 'contact'}<br>"
            f"<span class='small'>{c.get('phone','')}</span></div>",
            unsafe_allow_html=True
        )

        ok2, ep = api_call("GET", "/api/contacts/emergency-payload",
                           params={"user_id": int(user_id)}, timeout=6)
        if ok2 and (ep or {}).get("ok"):
            col1, col2, col3 = st.columns(3)
            with col1:
                st.link_button("📞 Call", ep["links"]["tel"], use_container_width=True, type="primary")
            with col2:
                st.link_button("✉️ SMS", ep["links"]["sms"], use_container_width=True)
            with col3:
                # WhatsApp with map URL
                wa_link = _wa_append_maps(ep["links"]["whatsapp"], maps_url=ep.get("maps_url"))
                st.link_button("🟢 WhatsApp", wa_link, use_container_width=True)
            if ep.get("maps_url"):
                st.markdown(
                    f"<span class='small'>Includes location: "
                    f"<a href='{ep['maps_url']}' target='_blank'>map link</a></span>",
                    unsafe_allow_html=True
                )
        else:
            show_error_block("Emergency links", ep)
    elif ok:
        st.warning("No primary contact yet. Ask the agent to add one.")
    else:
        show_error_block("Primary contact error", primary)

    st.divider()

    # Quick help
    st.markdown("**Quick help**")
    colA, colB = st.columns(2)
    with colA:
        if st.button("🆘 I’m lost — send my location", use_container_width=True, key="lost_btn"):
            ok3, ep = api_call("GET", "/api/contacts/emergency-payload",
                               params={"user_id": int(user_id)}, timeout=6)
            if ok3 and (ep or {}).get("ok"):
                st.success("Links ready below 👇 (tap to open)")
                st.link_button("📞 Call", ep["links"]["tel"], use_container_width=True, type="primary")
                st.link_button("✉️ SMS", ep["links"]["sms"], use_container_width=True)
                # WhatsApp with map URL if present
                wa_link = _wa_append_maps(ep["links"]["whatsapp"], maps_url=ep.get("maps_url"))
                st.link_button("🟢 WhatsApp", wa_link, use_container_width=True)
            else:
                show_error_block("Emergency payload error", ep)
    with colB:
        if st.button("🧭 Ask directions (use chat)", use_container_width=True, key="navtip"):
            st.info("Switch to Agent Chat and say: “How do I get to Home?”")

# ===================== Location Ping =====================
with tab_loc:
    st.subheader("Share your location")
    st.caption("This updates your last known location and runs safety checks.")

    col1, col2 = st.columns(2)
    with col1:
        lat = st.number_input("Latitude", value=1.352400, format="%.6f", key="loc_lat")
    with col2:
        lng = st.number_input("Longitude", value=103.819800, format="%.6f", key="loc_lng")

    battery = st.slider("Phone battery (%)", 0, 100, 50, key="loc_batt")
    notes = st.text_input("Notes (optional)", "", key="loc_notes")

    # Live map (with any saved route overlay)
    render_map(lat, lng)

    cA, cB, cC, cD = st.columns([1, 1, 1, 1])
    with cA:
        ping = st.button("📍 Ping location", type="primary", use_container_width=True, key="ping_loc")
    with cB:
        nearby = st.button("🆘 Find nearby help", use_container_width=True, key="nearby_help")
    with cC:
        elinks = st.button("🚨 Emergency links", use_container_width=True, key="emg_links")
    with cD:
        clr = st.button("🧹 Clear route", use_container_width=True, key="clear_route")

    if clr:
        st.session_state["route_coords"] = None
        st.session_state["route_to_name"] = None
        st.session_state["route_dest_point"] = None
        st.session_state["route_steps"] = []
        st.success("Route cleared.")
        st.rerun()

    # 1) Ping location -> /location/ping/enhanced
    if ping:
        with st.spinner("Updating location…"):
            okp, res = api_call(
                "POST", "/location/ping/enhanced",
                params={
                    "user_id": int(user_id),
                    "lat": float(lat),
                    "lng": float(lng),
                    "battery_level": int(battery),
                    "notes": notes or ""
                },
                timeout=12
            )
        if okp and (res or {}).get("ok"):
            st.success("Location updated.")

            # Extract agent prompt & tips from backend result
            dest = res.get("destination_prompt")
            if isinstance(dest, dict):
                prompt_msg = dest.get("message") or ""
            elif isinstance(dest, str):
                prompt_msg = dest
            else:
                prompt_msg = ""

            tips = (res or {}).get("recommendations") or []

            if prompt_msg:
                # store for Agent tab + auto jump
                st.session_state["auto_agent_message"] = prompt_msg
                st.session_state["auto_agent_tips"] = tips[:3]
                st.session_state["switch_to_agent"] = True

                # Jump to Agent tab (simulate click)
                st_html("""
                <script>
                const sleep = ms => new Promise(r => setTimeout(r, ms));
                (async () => {
                  for (let i=0;i<40;i++){
                    const tabs = Array.from(parent.document.querySelectorAll('button[role="tab"]'));
                    const btn = tabs.find(b => b.innerText.trim().includes('Agent Chat'));
                    if (btn){ btn.click(); break; }
                    await sleep(100);
                  }
                })();
                </script>
                """, height=0)
            else:
                st.info("All set. Dory will keep an eye on things.")
        else:
            show_error_block("Location update failed", res)

    # 2) Find nearby help -> /api/emergency/help-points
    if nearby:
        with st.spinner("Looking for nearby help…"):
            okh, hp = api_call(
                "GET", "/api/emergency/help-points",
                params={"lat": float(lat), "lon": float(lng), "radius_m": 2000},
                timeout=8
            )

        if okh and isinstance(hp, dict) and hp.get("ok"):
            # Normalize help_points into a flat list
            points = hp.get("help_points") or []
            if isinstance(points, dict):
                if all(isinstance(v, list) for v in points.values()):
                    flat = []
                    for v in points.values():
                        flat.extend(v)
                    points = flat
                else:
                    points = list(points.values())

            if not isinstance(points, list):
                points = []

            st.markdown("**Nearest help locations**")
            for i, pt in enumerate(points[:5]):
                name = pt.get("name", "Unknown")
                addr = pt.get("address", "")
                dist = pt.get("distance_m", "?")
                cat  = (pt.get("type") or "").upper()
                lat2 = pt.get("lat") or pt.get("latitude")
                lon2 = pt.get("lon") or pt.get("lng") or pt.get("longitude")

                with st.container():
                    st.markdown(
                        f"<div class='card'><b>{name}</b><br>"
                        f"{addr}<br><span class='small'>{cat} • {dist} m</span></div>",
                        unsafe_allow_html=True
                    )
                    # Button: compute & draw route to this help point
                    if st.button("Show route", key=f"route_{i}"):
                        # Prefer start-navigation endpoint
                        params_nav = {
                            "user_id": int(user_id),
                            "destination_name": name,
                            "current_lat": float(lat),
                            "current_lng": float(lng),
                        }
                        oknav, nav = api_call("GET", "/api/destinations/start-navigation",
                                              params=params_nav, timeout=15)
                        if not oknav:
                            oknav, nav = api_call("POST", "/api/destinations/start-navigation",
                                                  params=params_nav, timeout=15)

                        if oknav and isinstance(nav, dict) and nav.get("ok"):
                            # Try to pull polyline from various shapes
                            poly = None
                            if "navigation" in nav:
                                poly = ((nav["navigation"].get("route_info") or {}).get("polyline")
                                        or nav["navigation"].get("polyline"))
                            poly = poly or nav.get("polyline")
                            coords = _normalize_route_coords(poly) if poly else None

                            st.session_state["route_coords"] = coords
                            st.session_state["route_to_name"] = name
                            # If backend didn’t return dest point, fall back to the list item’s lat/lon
                            if lat2 is not None and lon2 is not None:
                                st.session_state["route_dest_point"] = {"lat": float(lat2), "lon": float(lon2)}
                            else:
                                st.session_state["route_dest_point"] = None
                            st.session_state["route_steps"] = (nav.get("navigation", {})
                                                               .get("text_directions", [])) or []
                            st.success(f"Route to {name} added to map above.")
                            st.experimental_rerun()
                        else:
                            show_error_block("Navigation error", nav)

            st.caption("Choose the nearest safe place. If urgent, call 995 (ambulance) or 999 (police).")
        else:
            show_error_block("Help-points error", hp)

    # 3) Emergency deep-links (call/SMS/WA) -> /api/contacts/emergency-payload
    if elinks:
        with st.spinner("Preparing links…"):
            oke, ep = api_call(
                "GET", "/api/contacts/emergency-payload",
                params={"user_id": int(user_id), "current_lat": float(lat), "current_lng": float(lng)},
                timeout=8
            )
        if oke and (ep or {}).get("ok"):
            c1, c2, c3 = st.columns(3)
            with c1:
                st.link_button("📞 Call", ep["links"]["tel"], use_container_width=True, type="primary")
            with c2:
                st.link_button("✉️ SMS", ep["links"]["sms"], use_container_width=True)
            with c3:
                # WhatsApp with current map link (or backend maps_url if provided)
                wa_link = _wa_append_maps(
                    ep["links"]["whatsapp"],
                    maps_url=ep.get("maps_url"),
                    lat=float(lat),
                    lng=float(lng),
                )
                st.link_button("🟢 WhatsApp", wa_link, use_container_width=True)
            if ep.get("maps_url"):
                st.markdown(
                    f"<span class='small'>Includes location: "
                    f"<a href='{ep['maps_url']}' target='_blank'>map link</a></span>",
                    unsafe_allow_html=True
                )
        else:
            show_error_block("Emergency links error", ep)

# ===================== Agent Chat =====================
with tab_agent:
    st.subheader("Talk to Dory")

    # If Location tab told us to switch & speak first, do it once
    if st.session_state.pop("switch_to_agent", False):
        auto_msg = st.session_state.pop("auto_agent_message", "")
        auto_tips = st.session_state.pop("auto_agent_tips", [])
        if auto_msg:
            with st.spinner("Dory is checking…"):
                ok_auto, data_auto = api_call(
                    "POST", "/api/agent/chat",
                    body={"user_id": int(user_id), "message": auto_msg},
                    timeout=int(agent_timeout)
                )
            st.markdown("---")
            if ok_auto:
                st.markdown("**Assistant**")
                st.success((data_auto or {}).get("final", auto_msg))
                if auto_tips:
                    st.markdown("**Tip:**")
                    for t in auto_tips:
                        st.markdown(f"- {t}")
            else:
                show_error_block("Agent error", data_auto)

    # Regular input
    msg = st.text_area(
        "Your message",
        placeholder='e.g., Add Sarah (+6598765432) as my primary emergency contact',
        height=100,
        label_visibility="collapsed",
        key="agent_msg",
    )

    col1, col2 = st.columns([1, 1])
    with col1:
        go = st.button("Send to agent", type="primary", use_container_width=True, key="agent_send")
    with col2:
        clear = st.button("Clear", use_container_width=True, key="agent_clear")

    if clear:
        st.rerun()

    if go and msg.strip():
        with st.spinner("Dory is working…"):
            ok, data = api_call(
                "POST", "/api/agent/chat",
                body={"user_id": int(user_id), "message": msg.strip()},
                timeout=int(agent_timeout),
            )
        st.markdown("---")
        if ok:
            st.markdown("**Assistant**")
            st.success((data or {}).get("final", "Done."))
            with st.expander("See steps (debug)"):
                st.json(data)
        else:
            show_error_block("Agent error", data)
