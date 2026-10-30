# AI congestion predictor: training results

**Not generated yet.** `ai/train.py` overwrites this file with the real numbers
the first time you train on data collected from your own network:

```bash
./run.sh --headless
python ai/collect_data.py --minutes 20     # drives traffic; controller logs ai/data/link_stats.csv
python ai/train.py                         # trains, compares, saves model, rewrites this file
./stop.sh && ./run.sh                      # controller loads ai/models/congestion_model.joblib
```

The report will contain:

* rows / links / train-test sample counts,
* MAE and RMSE (in % utilisation) for the moving-average fallback, Linear
  Regression and Random Forest on a time-ordered 80/20 split,
* which model was saved.

No numbers are filled in by hand: every figure in this file must come from a
training run on real collected data.
