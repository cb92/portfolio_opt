"""
run with command: streamlit run app.py
alternatively: python -m streamlit run app.py

With app deployed, changes will just show in the web browser on save if "Always update" is selected. 
When updates are required, app is run from top to bottom

References: 
https://docs.streamlit.io/get-started/fundamentals/main-concepts
https://docs.streamlit.io/get-started/fundamentals/advanced-concepts
"""
import streamlit as st
import pandas as pd
import time
import config
import optimization as opt

# col1, col2, col3 = st.columns(3)


# data = load_inputs()
# col1.dataframe(data.prices.head(10).style.highlight_max(axis=0)) # better to use st.dataframe for more flexibility
# chosen_assets = st.sidebar.selectbox('Choose asset prices to plot.', data.prices.columns)
# if st.sidebar.checkbox('Show chart'):
#     col2.line_chart(data.prices.head(10)[chosen_assets])

# # test widget, code rerun upon interacting
# x = col3.slider('x', key = 'slider1')
# col3.write(str(x) + ' squared is '  +str(x * x))
# col3.write(st.session_state.slider1)

# # Optimization Code
# 'Starting a long computation...'

# # Add a placeholder
# latest_iteration = st.sidebar.empty()
# bar = st.sidebar.progress(0)

# for i in range(100):
#   # Update the progress bar with each iteration.
#   latest_iteration.text(f'Iteration {i+1}')
#   bar.progress(i + 1)
#   time.sleep(0.1)

# '...and now we\'re done!'


@st.cache_data(ttl=3600)
def get_frames():
    return opt.fetch_frames()

frames = get_frames()
picked = st.multiselect('Assets', frames['assets']['ticker'])
data = opt.build_inputs(frames, tickers=picked or None)

try:
    result = opt.optimize(data, opt.Constraints(sector_max=0.30))
except opt.OptimizationFailed as e:
    st.error(str(e))
    st.stop()

st.metric('Sharpe (in-sample)', f'{result.sharpe:.2f}')
st.bar_chart(result.positions['weight'])
st.dataframe(result.sectors)