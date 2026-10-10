# MEMORY BATTLE – manual memory chooser
import streamlit as st, pandas as pd, os
from datetime import datetime
st.set_page_config(page_title="Memory Battle", layout="centered")

DATA = "mb_leaderboard.csv"
PASSWORD = "vortexmaster2025"
rating = ["Joy", "Nostalgia", "Excitement", "Lesson", "Clarity"]

default = ["First bike ride", "Beach sunset", "School cup win", "Road-trip 2022"]
if "memories" not in st.session_state:
    st.session_state.memories = default.copy()

def load():
    return pd.read_csv(DATA) if os.path.exists(DATA) else pd.DataFrame(columns=["Memory A","Memory B","Winner","Score A","Score B","Time",*rating])

def save(d): d.to_csv(DATA, index=False)

st.title("🧠 Memory Battle")

with st.sidebar.expander("➕ Add Memory"):
    m = st.text_input("Memory")
    if st.button("Add") and m:
        st.session_state.memories.append(m)
        st.success(f"{m} added!")

col1, col2 = st.columns(2)
mA = col1.selectbox("🔵 Memory A", st.session_state.memories, key="ma")
mB = col2.selectbox("🟣 Memory B", st.session_state.memories, key="mb")

if mA == mB:
    st.warning("Pick two different memories."); st.stop()

st.subheader(f"{mA} vs {mB}")

rA, rB = {}, {}
for f in rating:
    l, r = st.columns(2)
    rA[f] = l.slider(f"{f} – A", 1, 10, 5)
    rB[f] = r.slider(f"{f} – B", 1, 10, 5)

if st.button("Submit Battle"):
    sA, sB = sum(rA.values()), sum(rB.values())
    win = mA if sA > sB else mB if sB > sA else "Tie"
    row = {"Memory A": mA, "Memory B": mB, "Winner": win,
           "Score A": sA, "Score B": sB, "Time": datetime.now().strftime("%Y-%m-%d %H:%M"),
           **(rA if sA >= sB else rB)}
    df = pd.concat([load(), pd.DataFrame([row])], ignore_index=True)
    save(df); st.success(f"{win} wins!")

st.markdown("---")
st.subheader("🏆 Leaderboard")
st.dataframe(load().tail(20).iloc[::-1], use_container_width=True)

with st.expander("🔥 Reset"):
    if st.text_input("Password", type="password") == PASSWORD and st.button("Wipe"):
        os.remove(DATA) if os.path.exists(DATA) else None
        st.warning("All data cleared.")
        st.experimental_rerun()
