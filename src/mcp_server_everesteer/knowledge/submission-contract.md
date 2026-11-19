# Submission & Model Artifact Contract

## Predictions Format

- **Futures (Himalayas)**: Finite floats strictly inside **`[0.0, 1.0]`** covering every `instrument_id` in the open round's live universe. Call `validate_futures_submission` before `submit_futures_predictions` to catch coverage or range errors without writing.
- **Equities (Alps, unlaunched)**: Scores in `[-1.0, 1.0]` keyed by `ticker`.
- **Resubmission**: While a futures round window is open, resubmitting for the same `(model_id, exped)` safely overwrites the earlier manual submission (note: automated `.pkl` runs never overwrite a manual submission).
- **Never pass literal `"current"`** as `exped`; omit `exped` and this server automatically resolves the active round's real `exped`.

## Feature & Target Encoding

- Feature values are cross-sectional binned integers. Always inspect `feature_encoding` from `get_dataset_schema` rather than assuming a fixed number of bins.
- **`-1` always means MISSING** in every dataset. Never treat `-1` as an ordinal value below bin `0`; map it to `NaN` or a distinct category.
- `NaN` targets mean uncomputable; drop `NaN`-target rows when training.
- When saving Parquet predictions locally, disable dictionary encoding (`to_parquet(..., use_dictionary=False)`) to prevent high-cardinality string dictionary decode failures server-side.

## Model `.pkl` Callable Contract (`upload_model`, `submit_validation_diagnostics`, `submit_event_predictions`)

Only **one** artifact shape is accepted: a **cloudpickled callable** (`cloudpickle.dump`, not stdlib `pickle.dump`):

```python
def predict(live_features): ...


# Or with live benchmark reference (Series named `eiq_minera_model`):
def predict(live_features, live_benchmark_models): ...
```

- Return an exact `pandas.DataFrame` (single column, same index as `live_features`, finite values in **`[0.0, 1.0]`**).
- Refused shapes: bare `Series`, `numpy.ndarray`, fitted estimators on their own, dicts wrapping estimators, `torch` models (no PyTorch in inference sandbox), or custom ensemble containers. Build ensembles inside `predict(...)`.
- Declare `python_version` / `model_pkl_python_version` (`"major.minor"`) matching the Python interpreter that created the pickle.
- For auto-submit: `upload_model` -> poll `get_model_upload_status` until `validated` -> `set_auto_submit(enabled=True)` -> confirm `lane_active: true` in `list_models`.
