import pandas as pd
import numpy as np
import networkx as nx
import warnings
import time
from pathlib import Path


def safe_np_select(condlist, choicelist, default="Available"):
    """
    Safely execute conditional array selection without crashing on:
      - Empty condition lists or empty dataframes (ValueError: list of cases, can't be empty)
      - Length or shape mismatches between conditions and choices
      - Division-by-zero or NaN masks
    """
    if not condlist or not choicelist or len(condlist) != len(choicelist):
        if hasattr(default, "__len__") and not isinstance(default, (str, bytes)):
            return np.array(default)
        return default
    try:
        return np.select(condlist, choicelist, default=default)
    except Exception:
        try:
            n = len(condlist[0]) if hasattr(condlist[0], "__len__") else 1
            out = [default] * n
            for c, ch in zip(condlist, choicelist):
                for i in range(min(n, len(c))):
                    if out[i] == default and bool(c[i]):
                        out[i] = ch
            return np.array(out)
        except Exception:
            return default


def safe_float(val, default=0.0):
    """Safely convert value to float, returning default if None, NaN, or invalid."""
    if val is None or pd.isna(val):
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_int(val, default=0):
    """Safely convert value to int, returning default if None, NaN, or invalid."""
    if val is None or pd.isna(val):
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def safe_save_csv(df, filepath):
    """Safely write DataFrame to CSV file, ensuring parent directory exists."""
    try:
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)
        return True, "File saved successfully."
    except Exception as e:
        return False, f"Error saving CSV: {e}"


def risk_score(row, rainfall_multiplier=1.0, depth_multiplier=1.0):
    """
    Transparent illustrative flood risk score (0–100).
    Weighted combination:
      - 35% Severity (Low: 20, Medium: 55, High: 85, Critical: 100)
      - 25% Water depth (scaled 25 pts per meter up to 100)
      - 20% Rainfall (scaled 0.25 pts per mm up to 100)
      - 20% Urgency (scaled 10 pts per urgency unit up to 50)
    """
    if row is None:
        return 0.0
    sev_raw = str(row.get("severity", "Medium")).strip().capitalize()
    sev = {"Low": 20, "Medium": 55, "High": 85, "Critical": 100}.get(sev_raw, 50)
    
    depth_val = max(0.0, safe_float(row.get("water_depth_m", 0)))
    rain_val = max(0.0, safe_float(row.get("rainfall_mm", 0)))
    urgency_val = max(1.0, min(5.0, safe_float(row.get("urgency", 3), default=3.0)))

    depth = min(depth_val * 25.0 * max(0.0, float(rainfall_multiplier if depth_multiplier is None else depth_multiplier)), 100.0)
    rain = min(rain_val * 0.25 * max(0.0, float(rainfall_multiplier)), 100.0)
    urgency = urgency_val * 10.0

    score = 0.35 * sev + 0.25 * depth + 0.20 * rain + 0.20 * urgency
    return round(min(100.0, max(0.0, score)), 1)


def greedy_allocate(areas, stock, resource, stock_override=None):
    """
    Allocate scarce relief supplies using an urgency-first and risk-score greedy baseline.
    Parameters:
      - areas: DataFrame of affected areas (or filtered jurisdiction subset)
      - stock: DataFrame of stock resources (or None if stock_override provided)
      - resource: Name of resource (e.g. 'Water packages', 'Food packages', 'Medical kits')
      - stock_override: Optional integer to specify exact available stock directly
    Returns:
      (allocation_df, remaining_stock)
      Invariants:
      1. 0 <= qty <= demand for all areas
      2. sum(Allocated) <= total_available_stock
      3. sum(Allocated) + remaining_stock == total_available_stock
      4. Total Demand == Total Allocated + Total Unmet Demand
    """
    demand_col = {
        "Water packages": "water_demand",
        "Food packages": "food_demand",
        "Medical kits": "medical_demand",
    }.get(resource, "water_demand")

    if stock_override is not None:
        total = max(0, safe_int(stock_override, 0))
    elif stock is None or not isinstance(stock, pd.DataFrame) or stock.empty or "resource" not in stock.columns or "available" not in stock.columns:
        total = 0
    else:
        matches = stock.loc[stock["resource"].astype(str) == str(resource), "available"]
        total = safe_int(matches.iloc[0], 0) if not matches.empty else 0

    empty_cols = ["Area", "State", "District", "Urgency", "Risk score", "Demand", "Allocated", "Unmet demand"]
    if areas is None or not isinstance(areas, pd.DataFrame) or areas.empty:
        return pd.DataFrame(columns=empty_cols), max(0, total)

    df = areas.copy()
    if "risk_score" not in df.columns:
        df["risk_score"] = df.apply(risk_score, axis=1)

    # Sort primarily by urgency descending (5 to 1), then by risk_score descending (100 to 0)
    urgency_series = df["urgency"].apply(lambda v: safe_int(v, 3)) if "urgency" in df.columns else pd.Series(3, index=df.index)
    df["_urg_sort"] = urgency_series
    df = df.sort_values(["_urg_sort", "risk_score"], ascending=False).drop(columns=["_urg_sort"])

    remaining = max(0, total)
    rows = []
    for _, r in df.iterrows():
        area_name = str(r.get("area", f"Area-{len(rows)+1}"))
        st_name = str(r.get("state", "Tamil Nadu"))
        dt_name = str(r.get("district", "Chennai"))
        urg = safe_int(r.get("urgency", 3), 3)
        r_score = safe_float(r.get("risk_score", 0.0), 0.0)
        demand = max(0, safe_int(r.get(demand_col, 0), 0))
        
        # Transparent allocation calculation
        qty = max(0, min(demand, remaining))
        unmet = max(0, demand - qty)
        
        rows.append({
            "Area": area_name,
            "State": st_name,
            "District": dt_name,
            "Urgency": urg,
            "Risk score": r_score,
            "Demand": demand,
            "Allocated": qty,
            "Unmet demand": unmet
        })
        remaining -= qty

    return pd.DataFrame(rows), max(0, remaining)


def generate_dynamic_alerts(areas_df, shelters_df, roads_df, selected_scope="All India (National Scope)", selected_district="All Districts", selected_locality=None):
    """
    Generates structured, rule-based emergency alerts filtered strictly to the selected jurisdiction.
    Structured fields for each alert:
      - alert_id (str)
      - state (str)
      - district (str)
      - locality (str)
      - alert_type (str): 'Flood Risk', 'Road Closure', 'Hazardous Route', 'Shelter Overflow', 'Shelter Warning', 'Extreme Weather'
      - severity (str): 'Critical', 'High', 'Warning', 'Informational'
      - title (str)
      - description (str)
      - timestamp (str)
      - source (str)
      - data_status (str)
    """
    alerts = []
    curr_time_str = "2026-10-23 08:00 IST"

    def match_scope(row_state, row_district):
        if selected_scope != "All India (National Scope)":
            if str(row_state).strip().casefold() != selected_scope.strip().casefold():
                return False
            if selected_district != "All Districts":
                if str(row_district).strip().casefold() != selected_district.strip().casefold():
                    return False
        return True

    # 1. Flood Inundation & Weather Alerts from Areas
    if areas_df is not None and isinstance(areas_df, pd.DataFrame) and not areas_df.empty:
        df_a = areas_df.copy()
        if "risk_score" not in df_a.columns:
            df_a["risk_score"] = df_a.apply(risk_score, axis=1)

        for _, r in df_a.iterrows():
            r_state = str(r.get("state", "Tamil Nadu"))
            r_dist = str(r.get("district", "Chennai"))
            r_loc = str(r.get("locality", r.get("area", "")))
            if not match_scope(r_state, r_dist):
                continue

            r_score = safe_float(r.get("risk_score", 0.0), 0.0)
            depth = safe_float(r.get("water_depth_m", 0.0), 0.0)
            rain = safe_float(r.get("rainfall_mm", 0.0), 0.0)
            area_name = str(r.get("area", "Unknown Area"))
            source = str(r.get("data_source", "IMD & CWC Monitored Hydrology"))
            status_label = str(r.get("data_status", "Illustrative/Synthetic — Not Live Flood Data"))

            # Critical or High flood risk
            if r_score >= 80 or str(r.get("severity", "")).capitalize() == "Critical":
                alerts.append({
                    "alert_id": f"ALT-FLD-{r.get('area_id', area_name)}",
                    "state": r_state,
                    "district": r_dist,
                    "locality": r_loc,
                    "alert_type": "Flood Risk",
                    "severity": "Critical",
                    "title": f"Critical Flood Warning – {area_name}",
                    "description": f"Risk score {r_score:.1f}/100. Water depth: {depth:.1f}m, rainfall: {rain:.0f}mm. Priority deployment of rescue teams and supplies.",
                    "timestamp": curr_time_str,
                    "source": source,
                    "data_status": status_label
                })
            elif r_score >= 70 or str(r.get("severity", "")).capitalize() == "High":
                alerts.append({
                    "alert_id": f"ALT-FLD-{r.get('area_id', area_name)}",
                    "state": r_state,
                    "district": r_dist,
                    "locality": r_loc,
                    "alert_type": "Flood Risk",
                    "severity": "High",
                    "title": f"Elevated Flood Risk – {area_name}",
                    "description": f"Risk score {r_score:.1f}/100. Recorded depth: {depth:.1f}m, rainfall: {rain:.0f}mm. Waterlogging active.",
                    "timestamp": curr_time_str,
                    "source": source,
                    "data_status": status_label
                })

            # Extreme rainfall alert
            if rain >= 130.0:
                alerts.append({
                    "alert_id": f"ALT-RAIN-{r.get('area_id', area_name)}",
                    "state": r_state,
                    "district": r_dist,
                    "locality": r_loc,
                    "alert_type": "Extreme Weather",
                    "severity": "High",
                    "title": f"Heavy Rainfall Surge – {area_name}",
                    "description": f"Recorded {rain:.0f}mm precipitation. High potential for localized flash inundation.",
                    "timestamp": curr_time_str,
                    "source": source,
                    "data_status": status_label
                })

    # 2. Road Closures & Hazardous Routes
    if roads_df is not None and isinstance(roads_df, pd.DataFrame) and not roads_df.empty:
        for _, rd in roads_df.iterrows():
            rd_state = str(rd.get("state", "Tamil Nadu"))
            rd_dist = str(rd.get("district", "Chennai"))
            rd_loc = str(rd.get("locality", rd.get("start", "")))
            if not match_scope(rd_state, rd_dist):
                continue

            rd_stat = str(rd.get("status", "Open")).capitalize()
            start_pt = str(rd.get("start", ""))
            end_pt = str(rd.get("end", ""))
            source = str(rd.get("data_source", "Traffic Police & SDMA Road Survey"))
            status_label = str(rd.get("data_status", "Illustrative/Synthetic — Not Live Flood Data"))

            if rd_stat == "Blocked":
                alerts.append({
                    "alert_id": f"ALT-RD-{rd.get('road_id', 'RD')}",
                    "state": rd_state,
                    "district": rd_dist,
                    "locality": rd_loc,
                    "alert_type": "Road Closure",
                    "severity": "High",
                    "title": f"Road Blocked – {start_pt} ➔ {end_pt}",
                    "description": "Transit passage severed by deep inundation. Route planner automatically computes safe bypass.",
                    "timestamp": curr_time_str,
                    "source": source,
                    "data_status": status_label
                })
            elif rd_stat == "Hazardous":
                alerts.append({
                    "alert_id": f"ALT-RD-{rd.get('road_id', 'RD')}",
                    "state": rd_state,
                    "district": rd_dist,
                    "locality": rd_loc,
                    "alert_type": "Hazardous Route",
                    "severity": "Warning",
                    "title": f"Hazardous Passage – {start_pt} ➔ {end_pt}",
                    "description": f"Corridor partially submerged (hazard penalty {safe_float(rd.get('hazard_penalty', 4.0)):.1f}). Extreme caution required.",
                    "timestamp": curr_time_str,
                    "source": source,
                    "data_status": status_label
                })

    # 3. Shelter Capacity Breaches
    if shelters_df is not None and isinstance(shelters_df, pd.DataFrame) and not shelters_df.empty:
        for _, s in shelters_df.iterrows():
            s_state = str(s.get("state", "Tamil Nadu"))
            s_dist = str(s.get("district", "Chennai"))
            s_loc = str(s.get("locality", s.get("shelter", "")))
            if not match_scope(s_state, s_dist):
                continue

            cap = max(1, safe_int(s.get("capacity", 500), 500))
            occ = max(0, safe_int(s.get("occupancy", 0), 0))
            f_stat = str(s.get("status", "Open"))
            sh_stat = calculate_shelter_status(cap, occ, f_stat, near_full_threshold=0.90)
            sh_name = str(s.get("shelter", "Shelter"))
            avail = max(0, cap - occ)
            pct = (occ / cap * 100.0) if cap > 0 else 0.0
            source = str(s.get("data_source", "District Emergency Operations Center (DEOC)"))
            status_label = str(s.get("data_status", "Illustrative/Synthetic — Not Live Flood Data"))

            if sh_stat == "Full":
                alerts.append({
                    "alert_id": f"ALT-SH-{s.get('shelter_id', 'SH')}",
                    "state": s_state,
                    "district": s_dist,
                    "locality": s_loc,
                    "alert_type": "Shelter Overflow",
                    "severity": "Critical",
                    "title": f"Shelter at 100% Capacity – {sh_name}",
                    "description": f"Facility at capacity ({occ}/{cap} persons). 0 spaces available. Divert evacuees to adjacent facilities.",
                    "timestamp": curr_time_str,
                    "source": source,
                    "data_status": status_label
                })
            elif sh_stat == "Near Full":
                alerts.append({
                    "alert_id": f"ALT-SH-{s.get('shelter_id', 'SH')}",
                    "state": s_state,
                    "district": s_dist,
                    "locality": s_loc,
                    "alert_type": "Shelter Warning",
                    "severity": "Warning",
                    "title": f"Shelter Near Full ({pct:.0f}%) – {sh_name}",
                    "description": f"Occupancy at {occ}/{cap} persons. Only {avail} spaces remaining.",
                    "timestamp": curr_time_str,
                    "source": source,
                    "data_status": status_label
                })

    # Sort alerts by severity order: Critical -> High -> Warning
    sev_rank = {"Critical": 0, "High": 1, "Warning": 2, "Informational": 3}
    alerts.sort(key=lambda a: sev_rank.get(a.get("severity", "Warning"), 2))
    return alerts


def route_result(roads, start, end):
    """
    Find shortest/safest route excluding Blocked roads and penalizing Hazardous roads.
    Returns (route_nodes_list, total_cost) or (None, None) if no path exists.
    """
    start = str(start).strip()
    end = str(end).strip()

    if start == end and start:
        return [start], 0.0

    if roads is None or roads.empty:
        return None, None

    req_cols = {"start", "end", "distance_km", "status"}
    if not req_cols.issubset(set(roads.columns)):
        return None, None

    graph = nx.Graph()
    for _, r in roads.iterrows():
        status = str(r.get("status", "Open")).strip()
        if status == "Blocked":
            continue
        u = str(r["start"]).strip()
        v = str(r["end"]).strip()
        dist = max(0.01, safe_float(r.get("distance_km", 1.0), 1.0))
        penalty = max(0.0, safe_float(r.get("hazard_penalty", 0.0), 0.0)) if status == "Hazardous" else 0.0
        graph.add_edge(u, v, weight=dist + penalty, distance=dist, status=status)

    if start not in graph or end not in graph:
        return None, None

    try:
        route = nx.shortest_path(graph, start, end, weight="weight")
        cost = nx.shortest_path_length(graph, start, end, weight="weight")
        return route, round(cost, 2)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None, None


# =============================================================
# QUANTUM OPTIMIZATION (QUBO & QAOA) MODULE
# =============================================================

def build_qubo_matrices(scores, max_k, penalty_multiplier=None):
    """
    Builds the QUBO matrix Q and constant offset for the emergency payload allocation problem.
    Objective: Maximize sum(w_i * x_i)  <=> Minimize -sum(w_i * x_i)
    Subject to: sum(x_i) = k
    Equality Penalty: P(x) = lambda * (sum(x_i) - k)^2
    
    Standard Upper-Triangular Convention:
      Q_ii = -w_i + lambda * (1 - 2k)
      Q_ij = 2 * lambda for i < j
      Offset = lambda * k^2
    """
    n = len(scores)
    w = [safe_float(s, 0.0) for s in scores]
    max_w = max(w) if w else 1.0
    lam = float(penalty_multiplier) if penalty_multiplier is not None else round(max_w * 1.35, 1)

    c = [-w[i] + lam * (1.0 - 2.0 * max_k) for i in range(n)]

    Q = np.zeros((n, n))
    for i in range(n):
        Q[i, i] = c[i]
        for j in range(i + 1, n):
            Q[i, j] = 2.0 * lam

    offset = lam * (float(max_k) ** 2)
    return Q, offset, lam


def qubo_to_ising(Q, offset):
    """
    Converts QUBO matrix Q and offset into an Ising Cost Hamiltonian:
    H_C = sum h_i Z_i + sum J_ij Z_i Z_j + Const
    using the transformation x_i = (I - Z_i) / 2.
    """
    from qiskit.quantum_info import SparsePauliOp

    n = Q.shape[0]
    c = np.diag(Q).copy()
    
    q = {}
    for i in range(n):
        for j in range(i + 1, n):
            if abs(Q[i, j]) > 1e-9:
                q[(i, j)] = Q[i, j]

    h = {}
    for i in range(n):
        term = -c[i] / 2.0
        for j in range(n):
            if j != i:
                pair = (min(i, j), max(i, j))
                if pair in q:
                    term -= q[pair] / 4.0
        h[i] = term

    J = {pair: q[pair] / 4.0 for pair in q}
    const = offset + sum(c[i] / 2.0 for i in range(n)) + sum(q[pair] / 4.0 for pair in q)

    # Build Qiskit SparsePauliOp
    # Qiskit endianness: qubit i corresponds to string index (n - 1 - i)
    pauli_list = []
    if abs(const) > 1e-9:
        pauli_list.append(('I' * n, const))
    
    for i in range(n):
        if abs(h[i]) > 1e-9:
            p_str = ['I'] * n
            p_str[n - 1 - i] = 'Z'
            pauli_list.append((''.join(p_str), h[i]))

    for (i, j), val in J.items():
        if abs(val) > 1e-9:
            p_str = ['I'] * n
            p_str[n - 1 - i] = 'Z'
            p_str[n - 1 - j] = 'Z'
            pauli_list.append((''.join(p_str), val))

    H_cost = SparsePauliOp.from_list(pauli_list)
    return H_cost, h, J, const


def evaluate_qubo_solution(bitstring_or_x, scores, max_k, lam):
    """
    Calculates exact objective benefit, constraint penalty, and net QUBO energy for a candidate.
    """
    if isinstance(bitstring_or_x, str):
        # Qiskit bitstring: qubit 0 is rightmost bit
        x = [int(bitstring_or_x[len(bitstring_or_x) - 1 - i]) for i in range(len(bitstring_or_x))]
    else:
        x = list(bitstring_or_x)

    n = len(x)
    benefit = sum(float(scores[i]) * x[i] for i in range(n))
    selected_k = sum(x)
    violation = abs(selected_k - max_k)
    penalty = lam * ((selected_k - max_k) ** 2)
    qubo_energy = -benefit + penalty
    is_feasible = (violation == 0)
    selected_indices = [i for i in range(n) if x[i] == 1]

    return {
        "x": x,
        "selected_indices": selected_indices,
        "benefit_score": round(benefit, 2),
        "selected_count": selected_k,
        "target_count": max_k,
        "constraint_penalty": round(penalty, 2),
        "qubo_energy": round(qubo_energy, 2),
        "is_feasible": is_feasible,
        "violation": violation
    }


def solve_exact_brute_force(scores, max_k, lam):
    """
    Computes proven global optimum by exhaustive enumeration of all 2^n states.
    """
    n = len(scores)
    all_states = []
    for mask in range(1 << n):
        x = [(mask >> i) & 1 for i in range(n)]
        eval_res = evaluate_qubo_solution(x, scores, max_k, lam)
        all_states.append(eval_res)

    all_states.sort(key=lambda s: s["qubo_energy"])
    best_exact = all_states[0]

    feasible_states = [s for s in all_states if s["is_feasible"]]
    feasible_states.sort(key=lambda s: -s["benefit_score"])
    best_feasible = feasible_states[0] if feasible_states else best_exact

    return best_exact, best_feasible, all_states


def solve_classical_greedy(scores, max_k, lam):
    """
    Classical heuristic: priority sort by benefit score, taking top-k.
    """
    n = len(scores)
    sorted_indices = sorted(range(n), key=lambda i: float(scores[i]), reverse=True)
    x = [0] * n
    for idx in sorted_indices[:max_k]:
        x[idx] = 1
    return evaluate_qubo_solution(x, scores, max_k, lam)


def exact_binary_solution(scores, max_selected):
    """
    Backward-compatible brute-force exact solver for selection problem.
    Returns (best_score, chosen_indices_list).
    """
    if not scores or max_selected <= 0:
        return (0.0, [])
    scores = [safe_float(s, 0.0) for s in scores]
    n = min(len(scores), 18)
    sub_scores = scores[:n]
    max_k = min(max_selected, n)
    lam = max(sub_scores) * 1.35 if sub_scores else 100.0

    _, best_feasible, _ = solve_exact_brute_force(sub_scores, max_k, lam)
    return (best_feasible["benefit_score"], best_feasible["selected_indices"])


def run_qaoa_experiment(
    scores,
    max_k,
    p_reps=1,
    shots=1000,
    optimizer_name="COBYLA",
    maxiter=25,
    penalty_multiplier=None,
    seed=42
):
    """
    Executes an end-to-end local QAOA experiment using Qiskit 2.x native primitives.
    Returns comprehensive diagnostic results dictionary.
    """
    import warnings
    warnings.filterwarnings("ignore")

    t_start = time.time()
    n = len(scores)
    if n == 0 or max_k <= 0 or max_k > n:
        raise ValueError(f"Invalid problem parameters: n={n}, max_k={max_k}")

    # 1. Build QUBO
    Q, offset, lam = build_qubo_matrices(scores, max_k, penalty_multiplier)

    # 2. Convert to Ising Hamiltonian
    H_cost, h_dict, J_dict, const_val = qubo_to_ising(Q, offset)

    # 3. Construct QAOA Ansatz
    from qiskit.circuit.library import QAOAAnsatz
    from qiskit.primitives import StatevectorEstimator, StatevectorSampler
    from scipy.optimize import minimize

    ansatz = QAOAAnsatz(cost_operator=H_cost, reps=p_reps)
    num_params = 2 * p_reps

    # 4. Classical Optimization of QAOA Parameters
    estimator = StatevectorEstimator()
    eval_history = []

    def qaoa_objective(params):
        pub = (ansatz, H_cost, params)
        job = estimator.run([pub])
        val = float(job.result()[0].data.evs)
        eval_history.append(val)
        return val

    np.random.seed(seed if seed is not None else 42)
    initial_params = np.random.uniform(0.1, np.pi / 2.0, num_params)

    opt_method = optimizer_name if optimizer_name in ["COBYLA", "SLSQP", "Nelder-Mead"] else "COBYLA"
    opt_res = minimize(
        qaoa_objective,
        initial_params,
        method=opt_method,
        options={"maxiter": maxiter}
    )

    optimal_params = [float(v) for v in opt_res.x]
    final_energy_expectation = float(opt_res.fun)

    # 5. Measure and Sample Bitstrings
    assigned_circuit = ansatz.assign_parameters(optimal_params)
    meas_circuit = assigned_circuit.copy()
    meas_circuit.measure_all()

    sampler = StatevectorSampler(default_shots=shots)
    sample_job = sampler.run([meas_circuit])
    counts_dict = sample_job.result()[0].data.meas.get_counts()

    # 6. Decode Candidates and Rank
    total_shots = sum(counts_dict.values())
    candidate_list = []
    for bitstring, count in counts_dict.items():
        eval_dict = evaluate_qubo_solution(bitstring, scores, max_k, lam)
        eval_dict["bitstring"] = bitstring
        eval_dict["count"] = count
        eval_dict["probability"] = round(count / total_shots, 4)
        candidate_list.append(eval_dict)

    candidate_list.sort(key=lambda c: (-c["count"], c["qubo_energy"]))
    most_probable_candidate = candidate_list[0]
    best_energy_candidate = min(candidate_list, key=lambda c: c["qubo_energy"])

    # 7. Classical Baselines
    best_exact, best_feasible, _ = solve_exact_brute_force(scores, max_k, lam)
    greedy_sol = solve_classical_greedy(scores, max_k, lam)

    t_end = time.time()
    exec_time = round(t_end - t_start, 3)

    return {
        "success": True,
        "n_qubits": n,
        "p_reps": p_reps,
        "shots": shots,
        "optimizer": opt_method,
        "maxiter": maxiter,
        "num_evaluations": len(eval_history),
        "execution_time_sec": exec_time,
        "optimal_params": [round(float(v), 4) for v in optimal_params],
        "energy_expectation": round(final_energy_expectation, 3),
        "eval_history": [round(float(v), 2) for v in eval_history],
        "lambda_penalty": lam,
        "best_candidate": best_energy_candidate,
        "most_probable_candidate": most_probable_candidate,
        "all_candidates": candidate_list[:8],
        "exact_optimum": best_exact,
        "exact_feasible": best_feasible,
        "greedy_baseline": greedy_sol,
        "qubo_matrix": Q.tolist(),
        "circuit_depth": ansatz.depth(),
        "pauli_terms_count": len(H_cost)
    }


def solve_qaoa_binary_selection(
    scores,
    max_selected,
    maxiter=25,
    p_reps=1,
    shots=1000,
    optimizer_name="COBYLA",
    penalty_multiplier=None,
    seed=42
):
    """
    Standard entrypoint for QAOA binary optimization in FloodShield.
    Integrates run_qaoa_experiment and returns standard backward-compatible dictionary.
    """
    if not scores or max_selected <= 0:
        return {
            "success": True,
            "selected": [],
            "score": 0.0,
            "message": "No candidate areas or capacity zero.",
            "error": None
        }

    clean_scores = [safe_float(s, 0.0) for s in scores]
    try:
        exp_res = run_qaoa_experiment(
            scores=clean_scores,
            max_k=max_selected,
            p_reps=p_reps,
            shots=shots,
            optimizer_name=optimizer_name,
            maxiter=maxiter,
            penalty_multiplier=penalty_multiplier,
            seed=seed
        )
        best = exp_res["best_candidate"]
        return {
            "success": True,
            "selected": best["selected_indices"],
            "score": best["benefit_score"],
            "message": f"QAOA executed in {exp_res['execution_time_sec']}s via Qiskit Statevector simulator.",
            "error": None,
            **exp_res
        }
    except Exception as e:
        return {
            "success": False,
            "selected": [],
            "score": 0.0,
            "message": f"QAOA execution halted: {type(e).__name__}: {e}",
            "error": str(e)
        }


# =============================================================
# SHELTER MANAGEMENT HELPERS & RULES
# =============================================================

def calculate_shelter_status(capacity, occupancy, status_label="Open", near_full_threshold=0.90):
    """
    Computes shelter capacity status according to transparent documented rules:
      - Closed: If status is Closed
      - Full: occupancy >= capacity (or capacity <= 0)
      - Near Full: occupancy / capacity >= 0.90 (90% threshold) and < capacity
      - Available: occupancy / capacity < 0.90
    """
    clean_status = str(status_label).strip().capitalize()
    if clean_status == "Closed":
        return "Closed"
    cap = safe_int(capacity, 0)
    occ = safe_int(occupancy, 0)
    if cap <= 0:
        return "Full"
    if occ >= cap:
        return "Full"
    if (occ / cap) >= near_full_threshold:
        return "Near Full"
    return "Available"


def enrich_shelters_dataframe(df, near_full_threshold=0.90):
    """
    Enriches shelter DataFrame with:
      - capacity (int >= 0)
      - occupancy (int >= 0)
      - available_spaces (max(0, capacity - occupancy))
      - utilization_pct (round(occupancy / capacity * 100, 2))
      - calculated_status (Available, Near Full, Full, or Closed)
      - state, district, locality (preserved or defaulted safely)
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        cols = [
            "shelter_id", "shelter", "state", "district", "locality",
            "latitude", "longitude", "capacity", "occupancy",
            "status", "available_spaces", "utilization_pct", "calculated_status"
        ]
        return pd.DataFrame(columns=cols)

    enriched = df.copy()
    enriched["capacity"] = enriched["capacity"].apply(lambda v: max(0, safe_int(v, 0)))
    enriched["occupancy"] = enriched["occupancy"].apply(lambda v: max(0, safe_int(v, 0)))
    enriched["available_spaces"] = (enriched["capacity"] - enriched["occupancy"]).apply(lambda v: max(0, v))
    enriched["utilization_pct"] = (enriched["occupancy"] / enriched["capacity"].replace(0, np.nan) * 100.0).fillna(0.0).round(2)
    enriched["calculated_status"] = enriched.apply(
        lambda r: calculate_shelter_status(r["capacity"], r["occupancy"], r.get("status", "Open"), near_full_threshold=near_full_threshold),
        axis=1
    )
    if "state" not in enriched.columns:
        enriched["state"] = "Tamil Nadu"
    else:
        enriched["state"] = enriched["state"].fillna("Tamil Nadu").astype(str).str.strip()

    if "district" not in enriched.columns:
        enriched["district"] = "Chennai"
    else:
        enriched["district"] = enriched["district"].fillna("Chennai").astype(str).str.strip()

    if "locality" not in enriched.columns:
        enriched["locality"] = enriched["shelter"].astype(str)
    else:
        enriched["locality"] = enriched["locality"].fillna(enriched["shelter"]).astype(str).str.strip()

    return enriched


def validate_shelter_input(shelter_id, name, capacity, occupancy, status, state="Tamil Nadu", district="Chennai", locality="", latitude=13.05, longitude=80.21, existing_ids=None, is_edit=False, near_full_threshold=0.90):
    """
    Validates shelter inputs strictly:
      - Shelter ID and Name cannot be empty.
      - Capacity must be an integer greater than zero (> 0).
      - Occupancy cannot be negative and cannot exceed capacity (0 <= occupancy <= capacity).
      - Status must be 'Open' or 'Closed'.
      - ID must be unique when adding a new shelter.
    Returns: (is_valid: bool, error_message: str, clean_dict: dict)
    """
    s_id = str(shelter_id).strip()
    s_name = str(name).strip()

    if not s_id:
        return False, "Shelter ID is required.", None
    if not s_name:
        return False, "Shelter Name cannot be empty.", None

    if existing_ids and not is_edit:
        if s_id.casefold() in [str(x).casefold() for x in existing_ids]:
            return False, f"Shelter ID '{s_id}' already exists. Please choose a unique ID.", None

    cap = safe_int(capacity, -1)
    if cap <= 0:
        return False, "Total capacity must be an integer greater than zero (> 0).", None

    occ = safe_int(occupancy, -1)
    if occ < 0:
        return False, "Occupancy cannot be negative.", None
    if occ > cap:
        return False, f"Occupancy ({occ}) cannot exceed total capacity ({cap}).", None

    clean_status = str(status).strip().capitalize()
    if clean_status not in ["Open", "Closed"]:
        clean_status = "Open"

    lat = safe_float(latitude, 13.05)
    lon = safe_float(longitude, 80.21)

    s_state = str(state).strip() if state else "Tamil Nadu"
    s_dist = str(district).strip() if district else "Chennai"
    s_loc = str(locality).strip() if locality else s_name

    clean_dict = {
        "shelter_id": s_id,
        "shelter": s_name,
        "state": s_state,
        "district": s_dist,
        "locality": s_loc,
        "latitude": lat,
        "longitude": lon,
        "capacity": cap,
        "occupancy": occ,
        "status": clean_status
    }
    return True, "Valid shelter data.", clean_dict


# =============================================================
# DATA VALIDATION HELPERS FOR CSV IMPORT / EXPORT
# =============================================================

REQUIRED_AREAS_COLUMNS = [
    "area_id", "area", "latitude", "longitude", "population",
    "water_depth_m", "rainfall_mm", "severity", "urgency",
    "water_demand", "food_demand", "medical_demand"
]

REQUIRED_SHELTERS_COLUMNS = [
    "shelter_id", "shelter", "latitude", "longitude", "capacity", "occupancy", "status"
]

REQUIRED_ROADS_COLUMNS = [
    "road_id", "start", "end", "distance_km", "status", "hazard_penalty"
]

REQUIRED_STOCK_COLUMNS = [
    "resource", "available"
]


def validate_areas_df(df):
    """Validates and sanitizes affected areas DataFrame."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return False, "Data is empty.", None
    missing = [c for c in REQUIRED_AREAS_COLUMNS if c not in df.columns]
    if missing:
        return False, f"Missing required columns: {', '.join(missing)}", None
    sanitized = df.copy()
    sanitized["population"] = sanitized["population"].apply(lambda v: max(0, safe_int(v, 0)))
    sanitized["water_depth_m"] = sanitized["water_depth_m"].apply(lambda v: max(0.0, safe_float(v, 0.0)))
    sanitized["rainfall_mm"] = sanitized["rainfall_mm"].apply(lambda v: max(0.0, safe_float(v, 0.0)))
    sanitized["urgency"] = sanitized["urgency"].apply(lambda v: max(1, min(5, safe_int(v, 3))))
    sanitized["water_demand"] = sanitized["water_demand"].apply(lambda v: max(0, safe_int(v, 0)))
    sanitized["food_demand"] = sanitized["food_demand"].apply(lambda v: max(0, safe_int(v, 0)))
    sanitized["medical_demand"] = sanitized["medical_demand"].apply(lambda v: max(0, safe_int(v, 0)))
    sanitized["latitude"] = sanitized["latitude"].apply(lambda v: safe_float(v, 13.08))
    sanitized["longitude"] = sanitized["longitude"].apply(lambda v: safe_float(v, 80.27))
    sanitized["severity"] = sanitized["severity"].astype(str).str.strip().str.capitalize()
    if "state" not in sanitized.columns:
        sanitized["state"] = "Tamil Nadu"
    if "district" not in sanitized.columns:
        sanitized["district"] = "Chennai"
    if "locality" not in sanitized.columns:
        sanitized["locality"] = sanitized["area"]
    if "data_source" not in sanitized.columns:
        sanitized["data_source"] = "IMD & CWC Baseline"
    if "record_date" not in sanitized.columns:
        sanitized["record_date"] = "2024-08-15"
    if "data_status" not in sanitized.columns:
        sanitized["data_status"] = "Illustrative/Synthetic — Not Live Flood Data"
    if "event_type" not in sanitized.columns:
        sanitized["event_type"] = "Monitored Flood Area"
    return True, "Valid affected areas data.", sanitized


def validate_shelters_df(df):
    """Validates and sanitizes shelters DataFrame."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return False, "Data is empty.", None
    missing = [c for c in REQUIRED_SHELTERS_COLUMNS if c not in df.columns]
    if missing:
        return False, f"Missing required columns: {', '.join(missing)}", None
    sanitized = df.copy()
    sanitized["capacity"] = sanitized["capacity"].apply(lambda v: max(0, safe_int(v, 0)))
    sanitized["occupancy"] = sanitized["occupancy"].apply(lambda v: max(0, safe_int(v, 0)))
    sanitized["latitude"] = sanitized["latitude"].apply(lambda v: safe_float(v, 13.08))
    sanitized["longitude"] = sanitized["longitude"].apply(lambda v: safe_float(v, 80.27))
    sanitized["status"] = sanitized["status"].astype(str).str.strip().str.capitalize()
    if "state" not in sanitized.columns:
        sanitized["state"] = "Tamil Nadu"
    if "district" not in sanitized.columns:
        sanitized["district"] = "Chennai"
    if "locality" not in sanitized.columns:
        sanitized["locality"] = sanitized["shelter"]
    if "data_source" not in sanitized.columns:
        sanitized["data_source"] = "District Emergency Operations Center (DEOC)"
    if "record_date" not in sanitized.columns:
        sanitized["record_date"] = "2024-08-15"
    if "data_status" not in sanitized.columns:
        sanitized["data_status"] = "Illustrative/Synthetic — Not Live Flood Data"
    return True, "Valid shelters data.", sanitized


def validate_roads_df(df):
    """Validates and sanitizes roads DataFrame."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return False, "Data is empty.", None
    missing = [c for c in REQUIRED_ROADS_COLUMNS if c not in df.columns]
    if missing:
        return False, f"Missing required columns: {', '.join(missing)}", None
    sanitized = df.copy()
    sanitized["distance_km"] = sanitized["distance_km"].apply(lambda v: max(0.01, safe_float(v, 1.0)))
    sanitized["hazard_penalty"] = sanitized["hazard_penalty"].apply(lambda v: max(0.0, safe_float(v, 0.0)))
    sanitized["status"] = sanitized["status"].astype(str).str.strip().str.capitalize()
    if "state" not in sanitized.columns:
        sanitized["state"] = "Tamil Nadu"
    if "district" not in sanitized.columns:
        sanitized["district"] = "Chennai"
    if "locality" not in sanitized.columns:
        sanitized["locality"] = sanitized["start"]
    if "data_source" not in sanitized.columns:
        sanitized["data_source"] = "State Police & SDMA Road Survey"
    if "record_date" not in sanitized.columns:
        sanitized["record_date"] = "2024-08-15"
    if "data_status" not in sanitized.columns:
        sanitized["data_status"] = "Illustrative/Synthetic — Not Live Flood Data"
    return True, "Valid roads network data.", sanitized


def validate_stock_df(df):
    """Validates and sanitizes relief stock DataFrame."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return False, "Data is empty.", None
    missing = [c for c in REQUIRED_STOCK_COLUMNS if c not in df.columns]
    if missing:
        return False, f"Missing required columns: {', '.join(missing)}", None
    sanitized = df.copy()
    sanitized["available"] = sanitized["available"].apply(lambda v: max(0, safe_int(v, 0)))
    sanitized["resource"] = sanitized["resource"].astype(str).str.strip()
    return True, "Valid relief stock data.", sanitized


def get_map_bounds_and_center(df, selected_scope="All India (National Scope)", selected_district="All Districts", default_state_meta=None):
    """
    Cleans coordinates and computes optimal center latitude, longitude, and zoom.
    Invariants:
      - Validates and retains only valid coordinates within geographic bounding bounds (lat 6-38, lon 68-98).
      - Computes zoom and center appropriately for national overview, state scope, or district scope.
      - Returns (clean_df, center_dict, zoom_float).
    """
    clean_df = pd.DataFrame()
    if df is not None and isinstance(df, pd.DataFrame) and not df.empty and "latitude" in df.columns and "longitude" in df.columns:
        valid_mask = (
            pd.to_numeric(df["latitude"], errors="coerce").between(6.0, 38.0) &
            pd.to_numeric(df["longitude"], errors="coerce").between(68.0, 98.0)
        )
        clean_df = df[valid_mask].copy()

    if selected_scope == "All India (National Scope)":
        return clean_df, {"lat": 22.5, "lon": 82.0}, 3.8

    # Regional scope
    fallback_lat = 13.08
    fallback_lon = 80.27
    if default_state_meta and isinstance(default_state_meta, dict):
        fallback_lat = default_state_meta.get("lat", fallback_lat)
        fallback_lon = default_state_meta.get("lon", fallback_lon)

    if not clean_df.empty:
        c_lat = float(clean_df["latitude"].mean())
        c_lon = float(clean_df["longitude"].mean())
    else:
        c_lat = fallback_lat
        c_lon = fallback_lon

    if selected_district != "All Districts":
        zoom_val = 9.5
    else:
        zoom_val = 6.8

    return clean_df, {"lat": c_lat, "lon": c_lon}, zoom_val
