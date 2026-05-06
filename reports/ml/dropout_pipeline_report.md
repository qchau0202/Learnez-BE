# Dropout ML Pipeline Report

- run_id: de687937e491
- generated_at_utc: 2026-05-04T07:34:36.879896+00:00
- since_weeks: 20
- split: group_user
- status: completed

## composite

- model_path: `/Users/chou/Study/TDTU/DA-CNTT/523k0002_523k0021/source_code/Source/BE/ml/models/dropout_rf_composite.joblib`
- data_source: REAL_MONGO
- status: ok
- duration_sec: 6.443
- rows: train=11840 test=3965
- model_metrics: accuracy=0.9322 f1_macro=0.9315 roc_auc_ovr_weighted=0.9842
- demo_data_used: False
- smoke_predictions:
- baseline_majority: accuracy=0.4527 f1_macro=0.2078
- baseline_stratified: accuracy=0.3443 f1_macro=0.3221
  - idx=149 true=1 pred=1 proba={"0": 0.0004947562769216766, "1": 0.9987801856396282, "2": 0.000725058083449947}
  - idx=1769 true=1 pred=1 proba={"0": 0.00030214203904212796, "1": 0.998972799877508, "2": 0.000725058083449947}
  - idx=3641 true=0 pred=0 proba={"0": 0.9976648306112732, "1": 0.0009363530085619696, "2": 0.0013988163801645762}
  - idx=720 true=2 pred=2 proba={"0": 0.006986559437701808, "1": 0.004185050666198856, "2": 0.9888283898960993}
  - idx=325 true=1 pred=1 proba={"0": 0.0004947562769216766, "1": 0.9987801856396282, "2": 0.000725058083449947}
