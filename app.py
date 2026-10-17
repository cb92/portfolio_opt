"""
run with command: streamlit run app.py
alternatively: python -m streamlit run app.py

With app deployed, changes will just show in the web browser on save if "Always update" is selected. 
When updates are required, app is run from top to bottom

The page has three parts that run independently:
  1. Asset information  - shown automatically for the picked assets
  2. Optimization       - runs when "Run optimization" is clicked
  3. Share counts       - runs when "Calculate share numbers" is clicked (needs part 2)
Results of parts 2 and 3 live in st.session_state so clicking one part never clears another.
The sidebar holds the optimization constraints; they only take effect when "Set parameters" is clicked.

References: 
https://docs.streamlit.io/get-started/fundamentals/main-concepts
https://docs.streamlit.io/get-started/fundamentals/advanced-concepts
"""
import altair as alt
import streamlit as st
import pandas as pd
import time
import config
from pypfopt.discrete_allocation import DiscreteAllocation

import optimization as opt
from db import get_connection, queue_tickers

st.set_page_config(
    page_title="Mean-Variance Portfolio Optimizer",
    page_icon=":chart_with_upward_trend:",
    layout="wide")

st.title('Mean-Variance Portfolio Optimizer')
st.caption('Welcome to Christina\'s Mean-Variance Portfolio Optimization tool! This tool is designed to '
'help you choose a set of investments which suit your personal risk profile, and for which the Sharpe Ratio '
'is maximized across the investment options you provide. The Sharpe ratio takes into account both portfolio volatility '
'and expected returns, and expresses the excess expected portfolio return you can expect relative to portfolio volatility. '
'This tool proceeds in three steps: '
'\n1. **Setup**: Choose your candidate assets and set optimization parameters. The candidate assets can be chosen '
'from the current set in the database which will appear in the multi-select below. Be sure to choose assets that '
'represent a wide array of asset types, otherwise the optimizer will likely not converge. For each ticker, the top asset type is listed next to it. If '
'you don\'t know where to start, you can just select all of the assets available. A few things to keep in mind as you choose your assets: \n'
'* Most brokerages (ie. Vanguard, Fidelity) have different, proprietary funds which can be traded with lower expense ratios to track the same index.'
'For example, FXAIX (from Fidelity) and VOO (from Vanguard) both track the S&P. Depending on your preferred broker, you might choose one fund over another.  \n'
'* It has become increasingly popular to introduce mini-funds which allow investment into larger funds at a lower trading volume/expense ratio. These are better for '
'investors who intend to hold for a long time (which is probably you!). For the most popular such funds (ie. QQQM and QQQ), the same history is used in '
'the optimization step, so it doesn\'t matter which one you enter. But be aware of mini-funds when purchasing shares!'
'\n2. Run optimization by clicking the button below.'
'\n3. Optionally, if you want to calculate the number of shares you should buy/own given the size of your current portfolio, '
'you can get these numbers after the optimization completes. **Please note that I am a mathematician, not a tax professional! '
'Consider the tax implications of any financial moves before you make them.**')

# Helper function for dynamic color bar charts with percentage Y-axis
def plot_percentage_bar_chart(
        data, x_col, y_col, x_label, y_label, 
        label_angle=0, label_map=None, tooltip_col=None, tooltip_label=None):
    # Convert proportions to percentages for display
    df = data.copy().reset_index()
    if df.columns[0] == "index" or df.columns[0] == 0:
        df.rename(columns={df.columns[0]: x_col}, inplace=True)

    # Map the x-axis column values if a dictionary map is provided
    if label_map:
        df[x_col] = df[x_col].map(lambda val: label_map.get(val, val))

    tooltip = [
        alt.Tooltip(f"{x_col}:N", title=x_label),
        alt.Tooltip(f"{y_col}:Q", title=y_label, format=".1%"),
    ]
    if tooltip_col:
        tooltip.append(
            alt.Tooltip(f"{tooltip_col}:N", title=tooltip_label or tooltip_col)
        )

    chart = (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=alt.X(
                f"{x_col}:N",
                title=x_label,
                sort=None,
                axis=alt.Axis(
                    labelAngle=label_angle, labelLimit=300, labelOverlap=False
                ),  # Prevents cutoff
            ),
            y=alt.Y(
                f"{y_col}:Q",
                title=y_label,
                axis=alt.Axis(format="%"),  # Format Y-axis as percentage
            ),
            color=alt.Color(f"{x_col}:N", legend=None),  # Distinct bar colors
            tooltip=tooltip
        )
    )
    st.altair_chart(chart, width='stretch')


# ---------------------------------------------------------------------------
# Sidebar: optimization parameters (applied only when "Set parameters" is clicked)
# ---------------------------------------------------------------------------

def pct_slider(label, default, help, key, lo=0.0, hi=100.0, step=0.5, fmt='%.1f%%'):
    """A percent slider that takes and returns FRACTIONS (0.25 <-> 25%).
    `default` is a fraction, or a (low, high) tuple of fractions for a range slider."""
    if isinstance(default, tuple):
        value = tuple(round(100 * d, 2) for d in default)
    else:
        value = round(100 * default, 2)
    out = st.slider(label, lo, hi, value, step=step, format=fmt, help=help, key=key)
    if isinstance(out, tuple):
        return tuple(round(v / 100, 4) for v in out)
    return round(out / 100, 4)


defaults = opt.Constraints()                      # values from config.py
if 'constraints' not in st.session_state:
    st.session_state['constraints'] = defaults

with st.sidebar:
    st.header('Optimization parameters')
    st.caption('These parameters begin set to some typical values for a middle-aged person pursuing a relatively \
    aggressive investment strategy, but you can change them to reflect your own personal preferences. These \
    parameters  will significantly affect the outcome of the tool. you can adjust the sliders, then click "Set \
    parameters" at the bottom to apply them. All limits below are shares **of your invested assets** (everything except cash), \
    so changing the cash share doesn\'t change them.')
    with st.form('parameters'):
        min_w = 0
        max_w = pct_slider(
            'Max weight per asset', defaults.max_weight, key='p_weights',
            help='The largest share of your invested assets any single fund can hold. '
                 'For example, a max of 25% stops one fund from dominating. Most funds will be capped below this level by '
                 'other parameters, regardless, and this constraint is rarely binding unless it is set very low.')
        max_stock = pct_slider(
            'Max single stock', defaults.max_stock_pct, key='p_stock',
            help='The most your invested assets can hold in any one company, counting shares held '
                 'directly plus the company\'s weight inside each fund\'s top holdings. Due to the current size '
                 'of US megacaps and their dominance across several indices, setting this number below 2-2.5% '
                 'likely prevents optimizer convergence. 5 percent is usually a good limit to force conservative '
                 'diversification, but consider your own risk tolerance here.')
        sector_min, sector_max = pct_slider(
            'Sector exposure (min - max)', (defaults.sector_min, defaults.sector_max), key='p_sector',
            help='Every sector (technology, healthcare, energy, ...) must fall between these two '
                 'shares of your invested assets. A minimum above 0% forces exposure to every sector.')
        with st.expander('Asset class bounds'):
            st.caption('Each class must fall between its min and max share of your invested assets. '
                       'Domestic = US, international = developed non-US markets; '
                       'large / mid / small refer to company size. The ideal breakdown here depends on your age, risk tolerance and investment theories, '
                       'see [Vanguard target date glidepath](https://workplace.vanguard.com/investment/strategies/tdf-glide-path.html) for guidance. '
                       'Be sure to maintain some tolerances in these numbers or the optimizer may not converge.')
            class_bounds = {
                c: pct_slider(config.ASSET_CLASS_PRETTY_LABELS.get(c, c), defaults.class_bounds.get(c, (0.0, 1.0)), key=f'p_class_{c}',
                              help=f'Minimum and maximum share of the portfolio in {config.ASSET_CLASS_PRETTY_LABELS.get(c, c).lower()}.')
                for c in config.ASSET_CLASSES
            }
        pct_cash = pct_slider(
            'Cash share', defaults.pct_risk_free, key='p_cash', hi=90.0,
            help='Share of the whole portfolio held in cash earning the risk-free rate; the rest is invested. '
                 'All other limits here apply to the invested part only. Consider both [Vanguard target date glidepath](https://workplace.vanguard.com/investment/strategies/tdf-glide-path.html) for guidance '
                 'and your own personal short term goals (such as homeownership) while setting this number.')
        rf_rate = pct_slider(
            'Assumed risk-free rate (annual)', defaults.risk_free_rate, key='p_rf', hi=10.0, step=0.1,
            help='The yearly return assumed for a safe investment. Sharpe ratios measure return above '
                 'this rate per unit of risk, and any cash share earns it. A good value '
                 'here would be the 10 year bond yield, but you can enter any assumption you like.')
        submitted = st.form_submit_button('Set parameters')

    if submitted:
        st.session_state['constraints'] = opt.Constraints(
            min_weight=min_w, max_weight=max_w, max_stock_pct=max_stock,
            sector_min=sector_min, sector_max=sector_max, class_bounds=class_bounds,
            pct_risk_free=pct_cash, risk_free_rate=rf_rate)
        st.toast('Parameters updated. Click "Run optimization" to use them.')



constraints = st.session_state['constraints']      # what the optimizer will use


@st.cache_data(ttl=3600)
def get_frames():
    return opt.fetch_frames()


# ---------------------------------------------------------------------------
# Part 1: asset information
# ---------------------------------------------------------------------------
def render_asset_info(data, disp, picked):
    st.subheader('Relative Adjusted Price History over shared period')
    '**Relative** refers to the fact that we are setting the value at the start of the period to 1.0 for every asset. **Adjusted** means that we are tracking dividend-adjusted rather than raw returns. The shared period is the time frame over which all chosen assets have price history.'
    st.line_chart(data.prices.div(data.prices.iloc[0], axis=1), x_label='Date', y_label='Relative Adjusted Price')
    disp['Top Asset Class'] = disp['Top Asset Class'].map(config.ASSET_CLASS_PRETTY_LABELS)
    st.dataframe(disp.loc[picked])


# ---------------------------------------------------------------------------
# Part 2: optimization results (reads a saved result; never recomputes)
# ---------------------------------------------------------------------------
def render_optimization(result, ef_points, data, risk_free_rate):
    col1, col2, col3 = st.columns(3)
    col1.metric('Sharpe (in-sample)', f'{result.sharpe:.2f}')
    col2.metric('Expected Annual Return', f'{100.0*result.expected_return:.2f}%')
    col3.metric('Weighted Expense Ratio', f'{100.0*result.expense_ratio:.2f}%')

    # 1. Asset and Portfolio Sharpe Ratios
    st.subheader("Asset and Portfolio Sharpe Ratios")
    st.caption('Typically, the portolio Sharpe ratio is higher than most or all of the constituent investments. '
    'This reflects the fact that engaging in diversification through investment in less correlated assets can lower '
    'overall portfolio volatility while keeping a similar level of expected return.')
    # same window and risk-free rate as the portfolio's Sharpe, so the bars are comparable
    asset_sharpes = data.prices.apply(lambda s: opt.sharpe_ratio(s, risk_free_rate))
    sharpe_series = pd.concat([asset_sharpes, pd.Series({"Optimized Portfolio": result.sharpe})])
    df_sharpe = sharpe_series.reset_index()
    df_sharpe.columns = ["Asset", "Sharpe Ratio"]

    sharpe_chart = (
        alt.Chart(df_sharpe)
        .mark_bar()
        .encode(
            x=alt.X("Asset:N", title="Asset / Portfolio", sort=None),
            y=alt.Y("Sharpe Ratio:Q", title="Sharpe Ratio"),
            color=alt.condition(
                alt.datum.Asset == "Optimized Portfolio",
                alt.value("#FF4B4B"),  # Highlight color for Portfolio
                alt.value("#1F77B4"),  # Default color for individual assets
            ),
        )
    )
    st.altair_chart(sharpe_chart, width='stretch')

    # 2. Asset Weights in Optimized Portfolio
    st.subheader("Asset Weights in Optimized Portfolio")
    names = pd.Series(data.metadata['longname'].values, index = data.metadata['ticker'], name = 'Name')
    plot_percentage_bar_chart(
        pd.concat([result.positions["weight"], names], axis=1, join='inner'),
        x_col="Asset",
        y_col="weight",
        x_label="Asset",
        y_label="Portfolio Weight", 
        tooltip_col='Name'
    )
    # 3. Sector Exposures in Optimized Portfolio
    st.subheader("Sector Exposures in Optimized Portfolio")
    plot_percentage_bar_chart(
        result.sectors["exposure"],
        x_col="sector",
        y_col="exposure",
        x_label="Sector",
        y_label="Exposure (% of invested assets)",
        label_angle=-45,  # Angle labels to prevent cutoff
        label_map = config.SECTOR_PRETTY_LABELS
    )
    # 4. Asset Classes in Optimized Portfolio
    st.subheader("Asset Classes in Optimized Portfolio")
    plot_percentage_bar_chart(
        result.asset_classes["exposure"],
        x_col="asset_class",
        y_col="exposure",
        x_label="Asset Class",
        y_label="Exposure (% of invested assets)",
        label_angle=-45,  # Angle labels to prevent cutoff
        label_map = config.ASSET_CLASS_PRETTY_LABELS
    )
    # 5. Style Balance in Optimized Portfolio
    st.subheader("Style Balance in Optimized Portfolio")
    plot_percentage_bar_chart(
        result.styles["exposure"],
        x_col="style",
        y_col="exposure",
        x_label="Investment Style",
        y_label="Exposure (% of invested assets)",
        label_map=config.STYLE_PRETTY_LABELS
    )

    # 6. Top Holdings in Optimized Portfolio (Sorted Descending)
    st.subheader("Top Holdings in Optimized Portfolio")
    top_10 = (
        result.top_holdings.sort_values(ascending=False)
        .head(10).rename('weight')
    )
    plot_percentage_bar_chart(
        top_10,
        x_col="underlying_ticker",
        y_col="weight",
        x_label="Holding",
        y_label="Share of Invested Assets",
    )

    # 7. Efficient frontier
    st.subheader("Efficient Frontier (Possible Portfolios of minimum variance for fixed return)")
    st.caption("This graph shows possible outcomes in terms of expected return/volatility pairings, which "
    "are solved for by setting the expected return to several different values. The optimizer above chooses the point "
    "from this set, known as the efficient frontier, at which the ratio between the expected return and volatility "
    "is the highest.")
    chart = (
        alt.Chart(ef_points)
        .mark_circle(size=60)
        .encode(
            x=alt.X('volatility:Q', scale=alt.Scale(zero=False), title='Volatility (Risk)'),
            y=alt.Y('expected_return:Q', scale=alt.Scale(zero=False), title='Expected Return'),
            tooltip=['volatility', 'expected_return']
        )
        .interactive() # Adds pan & zoom
    )
    st.altair_chart(chart, width='stretch')

    # 8. Backtesting return
    st.subheader('Portfolio Backtesting')
    st.caption("The graphs below show how this portfolio would have performed if it had been selected 10 years ago and annually rebalanced to keep the weights stable. The "
        "first chart shows the growth of the portfolio, the cost basis, and the taxes paid over time. The second shows the overall performance "
        "of the chosen portfolio relative to the individual assets that comprise it. Keep in mind that the individual assets would not have taxes paid/basis growth.")
    st.line_chart(result.backtest_results)

    # 9. Backtested Portfolio vs. Constituent Asset Relative Adjusted Returns
    w = result.weights.drop('CASH', errors='ignore')
    active_assets = w[w > 0].index.tolist()
    
    # Resample asset prices to month-end to match backtest resolution
    prices_m = data.prices[active_assets].resample('ME').last().dropna()
    asset_rel = prices_m.div(prices_m.iloc[0], axis=1)
    port_rel = (result.backtest_results['portfolio_value'] / result.backtest_results['portfolio_value'].iloc[0]).rename('Optimized Portfolio')
    
    combined_df = asset_rel.join(port_rel, how='inner').reset_index().melt(
        id_vars=['price_date'], var_name='Asset', value_name='Relative Value'
    )
        
    # Base chart encoding
    base = alt.Chart(combined_df).encode(
        x=alt.X('price_date:T', title='Date'),
        y=alt.Y('Relative Value:Q', title='Relative Adjusted Price'),
        color=alt.Color('Asset:N', title='Asset')
    )
    
    # Individual asset lines (thin)
    asset_lines = base.transform_filter(
        alt.datum.Asset != 'Optimized Portfolio'
    ).mark_line(strokeWidth=1)
    
    # Optimized Portfolio line (thick white line)
    portfolio_line = base.transform_filter(
        alt.datum.Asset == 'Optimized Portfolio'
    ).mark_line(
        strokeWidth=4
    )
    
    st.altair_chart(asset_lines + portfolio_line, width='stretch')


# ---------------------------------------------------------------------------
# Part 3: share counts (reads a saved allocation; never recomputes)
# ---------------------------------------------------------------------------
def render_allocation(allocation, leftover):
    st.metric('Leftover cash', f'${leftover:,.2f}')
    st.dataframe(pd.Series(allocation, name='shares'), width="content")
    st.caption('Prices are the latest stored weekly close, not live quotes.')


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

frames = get_frames()

# asset choice and submission
col1, col2 = st.columns([0.7,0.3])
with col1:
    st.subheader('Assets')
    st.caption('The pre-selected assets are a good start, but select more if you want!')
    # 1. Map your tickers to their respective top asset classes for quick lookup
    asset_map = dict(zip(frames['assets']['ticker'], frames['assets']['top_asset_class']))

    # 2. Pass the raw tickers as options, but format how they render visually
    picked = st.multiselect(
        label='Assets', 
        options=frames['assets']['ticker'], 
        format_func=lambda x: f"{x} ({config.ASSET_CLASS_PRETTY_LABELS.get(asset_map.get(x), asset_map.get(x, 'Unknown'))})",        
        label_visibility="collapsed", 
        default = ['DIA','FEZ','FNDF','IEFA','IEUR','IPAC','ONEQ','QQQM','RWK','SPEM',
            'VDE','VEA','VEIRX','VHT','VOO','VTI','VWO', 'XLE','XLV',  'XMHQ', 'XMMO', 'XSVM']
    )

with col2: 
    st.subheader('New Asset Requests')
    with st.form('tickers'):
        new_ticker = st.text_input("Ticker to Add", placeholder="", help='If there is a ticker you want included that you don\'t see in the list to the left, '
            'you can submit it for inclusion here. Because one of the packages used to pull stock and fund history (`mstarpy`) blocks automatic requests '
            'without a headless browser, this process must be handled offline (for now). I will empty the queue with some regularity!')        
        submitted_ticker = st.form_submit_button('Submit ticker')
        if submitted_ticker: 
            if 'submission_counter' not in st.session_state:
                st.session_state.submission_counter = 0
            
            if st.session_state.submission_counter < 10:
                # set up database connection
                cnx = get_connection()
                cur = cnx.cursor()
                try:
                    queued, existing, invalid = queue_tickers(cur, [new_ticker])
                    cnx.commit()
                    if queued == 1:
                        st.session_state.submission_counter += 1
                        st.code('Ticker inserted into queue: ' + new_ticker + 
                            ' \nTotal successful submissions: '+str(st.session_state.submission_counter), 
                            language='markdown')
                    elif existing == 1: 
                        st.code('Ticker is already in queue: ' + new_ticker, language='markdown')
                    elif len(invalid) >= 1:
                        st.code('Invalid ticker not inserted: ' + new_ticker, language='markdown')
                except Exception as e:
                    cnx.rollback()
                    st.write('Failed to queue tickers: ' + new_ticker)
                finally: 
                    cur.close()
                    cnx.close()
            else: 
                st.write('You have hit the submission limit, sorry!')

if not picked:
    st.info('Pick one or more assets to begin.')
    st.caption(
        '_Important notes: \n'
        'This is a financial planning tool. Financial planning tools and educational content do not constitute personalized investment, tax, or legal advice. '
        'The presence of this application does not imply a certain level of skill or training. '
        'The projections, simulations, and outputs shown are best estimates based on the funds, optimization bounds and other inputs you provide. '
        'They are not guarantees of future performance. This model relies on historical data and assumed return distributions that may not reflect actual future returns. '
        'Past performance is not indicative of future results. Always consult a qualified professional before making material financial decisions._'
        ''
        '\n\n[Github repo](https://github.com/cb92/portfolio_opt)     |     [My Linkedin](https://www.linkedin.com/in/cmbrasco/)'
        )
    st.stop()

selection = tuple(sorted(picked))   # identifies "which assets" a saved result belongs to

try:
    data = opt.build_inputs(frames, tickers=picked)
except opt.NoDataError as e:
    st.error(str(e))
    st.stop()

disp = data.metadata.rename(columns={
            "security_type": "Security Type", 
            "expense_ratio": "Expense Ratio",
            "longname":      "Name",
            "top_asset_class": "Top Asset Class",
            "price_target_median": "Median Price Target",
            "price_target_mean": "Mean Price Target",
            "price_target_low": "Low Price Target",
            "price_target_hi": "High Price Target",
            "avg_analyst_rating": "Analyst Rating",
            #"long_business_summary": "Description",
            "sharpe_ratio": "Sharpe ratio",}, inplace=False).set_index('ticker')

# --- Part 1: asset information (automatic) ---
render_asset_info(data, disp, picked)

# --- Part 2: optimization (on demand) ---
st.header('Optimization')
if st.button('Run optimization', type='primary'):
    try:
        st.session_state['opt'] = {
            'selection': selection,
            'constraints': constraints,
            'result': opt.optimize(data, constraints),
            'ef_points': opt.efficient_frontier_points(data, constraints),
            'run_id': time.time(),          # lets part 3 tell when its result is out of date
        }
    except opt.OptimizationFailed as e:
        st.session_state.pop('opt', None)
        st.error(str(e))

saved = st.session_state.get('opt')
opt_is_current = (saved is not None and saved['selection'] == selection
                  and saved['constraints'] == constraints)
if opt_is_current:
    render_optimization(saved['result'], saved['ef_points'], data, constraints.risk_free_rate)
elif saved is not None:
    st.info('Your asset selection or parameters changed. Click "Run optimization" to update these results.')

# --- Part 3: share counts (on demand, needs a current optimization) ---
st.header('Share counts')
if not opt_is_current:
    st.caption('Run the optimization above first.')
else:
    p_size = st.number_input("Portfolio size", min_value=10000, max_value=None)
    if st.button('Calculate share numbers'):
        w = saved['result'].positions['weight'].drop('CASH', errors='ignore')   # cash isn't bought as shares
        allocation, leftover = DiscreteAllocation((w / w.sum()).to_dict(), data.prices.iloc[-1], total_portfolio_value=p_size * w.sum()).greedy_portfolio()
        st.session_state['alloc'] = {'run_id': saved['run_id'], 'p_size': p_size, 'allocation': allocation, 'leftover': leftover + p_size * (1 - w.sum())}

    alloc = st.session_state.get('alloc')
    if alloc and alloc['run_id'] == saved['run_id'] and alloc['p_size'] == p_size:
        render_allocation(alloc['allocation'], alloc['leftover'])
    elif alloc and alloc['run_id'] == saved['run_id']:
        st.caption('Portfolio size changed. Click "Calculate share numbers" to update.')

st.caption(
'_Important notes: \n'
'This is a financial planning tool. Financial planning tools and educational content do not constitute personalized investment, tax, or legal advice. '
'The presence of this application does not imply a certain level of skill or training. '
'The projections, simulations, and outputs shown are best estimates based on the funds, optimization bounds and other inputs you provide. '
'They are not guarantees of future performance. This model relies on historical data and assumed return distributions that may not reflect actual future returns. '
'Past performance is not indicative of future results. Always consult a qualified financial or tax professional before making material financial decisions._'
''
'\n\n[Github repo](https://github.com/cb92/portfolio_opt)     |     [My Linkedin](https://www.linkedin.com/in/cmbrasco/)'
)