#!/usr/bin/env python
# coding: utf-8

# # WLTests Results
# Analyses and visualises results generated by a wltest

# In[ ]:


import logging
from IPython.display import display

from lisa.wa_results_collector import WaResultsCollector
import pandas as pd

#%pylab inline


# ## Results analysis and metrics collection

# In[ ]:


collector = WaResultsCollector(
    
    # WLTests results folder:
    base_dir='/root/output/', # Base path of your results folders
    wa_dirs='wa',   # Parse only folder matching this regexp

    # Results to collect:
    parse_traces=False,                # Enable trace parsing only to get more metrics
                                       # NOTE: results generation will take more times
    
    # Kernel tree used for the tests
    #kernel_repo_path='/path/to/your/linux/sources/tree'
    
    #Don't display charts
    display_charts=False
)


# ## Collected metrics

# In[ ]:


df = collector.results_df
logging.info("Metrics available for plots and analysis:")
for metric in df.metric.unique().tolist():
    logging.info("   %s", metric)
results_nrj = pd.DataFrame()


# # Jankbench

# ## Total Frame Duration

# In[ ]:


results = pd.DataFrame()
for test in collector.tests(workload='jankbench'):
    logging.info("Results for: %s", test)
    plot,result = collector.report(workload='jankbench', metric='frame_total_duration',
                     test="^{}$".format(test), sort_on='99%', ascending=True)
    results = results.append(result)
results.to_csv("jankbench.csv")


# ## Energy

# In[ ]:


for test in collector.tests(workload='jankbench'):
    logging.info("Results for: %s", test)
    plot,result = collector.report(workload='jankbench', metric='VDD_total_energy',
                     test="^{}$".format(test), sort_on='mean', ascending=True)
    results_nrj = results_nrj.append(result)


# In[ ]:


for test in collector.tests(workload='jankbench'):
    logging.info("Results for: %s", test)
    plot,result = collector.report(workload='jankbench', metric='VDD_average_power',
                     test="^{}$".format(test), sort_on='mean', ascending=True)
    results_nrj = results_nrj.append(result)


# ## Frames Duration CDF

# In[ ]:


for test in collector.tests(workload='jankbench'):
    logging.info("Results for: %s", test)
    collector.plot_cdf(workload='jankbench', metric='frame_total_duration',
                       test="^{}$".format(test), threshold=16)


# # Exoplayer

# ## Dropped Frames

# In[ ]:


results = pd.DataFrame()
for test in collector.tests(workload='exoplayer'):
    logging.info("Results for: %s", test)
    plot,result = collector.report(workload='exoplayer', metric='exoplayer_dropped_frames',
                     test=test, sort_on='99%', ascending=True)
    results = results.append(result)
results.to_csv("exoplayer.csv")


# ## Energy

# In[ ]:


for test in collector.tests(workload='exoplayer'):
    logging.info("Results for: %s", test)
    plot,result = collector.report(workload='exoplayer', tag='mov_*', metric='VDD_total_energy',
                     test=test, sort_on='mean', ascending=True)
    results_nrj = results_nrj.append(result)


# In[ ]:


for test in collector.tests(workload='exoplayer'):
    logging.info("Results for: %s", test)
    plot,result = collector.report(workload='exoplayer', tag='ogg_*', metric='VDD_average_power',
                     test=test, sort_on='mean', ascending=True)
    results_nrj = results_nrj.append(result)


# In[ ]:


for test in collector.tests(workload='exoplayer'):
    logging.info("Results for: %s", test)
    plot,result = collector.report(workload='exoplayer', tag='mov_*', metric='VDD_total_energy',
                     test=test, sort_on='mean', ascending=True)
    results_nrj = results_nrj.append(result)


# In[ ]:


for test in collector.tests(workload='exoplayer'):
    logging.info("Results for: %s", test)
    plot,result = collector.report(workload='exoplayer', tag='mov_*', metric='VDD_average_power',
                     test=test, sort_on='mean', ascending=True)
    results_nrj = results_nrj.append(result)


# # Homescreen

# In[ ]:


for test in collector.tests(workload='homescreen'):
    logging.info("Results for: %s", test)
    plot,result = collector.report(workload='homescreen', metric='VDD_total_energy',
                     test=test, sort_on='mean', ascending=True)
    results_nrj = results_nrj.append(result)


# In[ ]:


for test in collector.tests(workload='homescreen'):
    logging.info("Results for: %s", test)
    plot,result = collector.report(workload='homescreen', metric='VDD_average_power',
                     test=test, sort_on='mean', ascending=True)
    results_nrj = results_nrj.append(result)


# In[ ]:


for test in collector.tests(workload='idle'):
    logging.info("Results for: %s", test)
    plot,result = collector.report(workload='idle', metric='VDD_total_energy',
                     test=test, sort_on='mean', ascending=True)
    results_nrj = results_nrj.append(result)


# In[ ]:


for test in collector.tests(workload='idle'):
    logging.info("Results for: %s", test)
    plot,result = collector.report(workload='idle', metric='VDD_average_power',
                     test=test, sort_on='mean', ascending=True)
    results_nrj = results_nrj.append(result)


# # Vellamo

# In[ ]:


pm_df = df[df.workload == 'vellamo']


# ## Html5

# In[ ]:


results = pd.DataFrame()
pm_scores = [m for m in pm_df.metric.unique().tolist() if m.startswith('html5')]
total = None
for metric in pm_scores:
    plot,result = collector.report(workload='vellamo', metric=metric,
                     sort_on='mean', ascending=False)
    logging.info("Results for: %s", metric)
    results = results.append(result)
    
    result.rename(columns={metric: 'html5'}, inplace=True)
    if total is None:
        total = result
    else:
        total += result

logging.info("Results for: html5")
results = results.append(total)
results.to_csv("vellamo_html5.csv")


# ## Metal

# In[ ]:


results = pd.DataFrame()
pm_scores = [m for m in pm_df.metric.unique().tolist() if m.startswith('metal')]
total = None
for metric in pm_scores:
    plot,result = collector.report(workload='vellamo', metric=metric,
                     sort_on='mean', ascending=False)
    logging.info("Results for: %s", metric)
    results = results.append(result)
    
    result.rename(columns={metric: 'metal'}, inplace=True)
    if total is None:
        total = result
    else:
        total += result

logging.info("Results for: metal")
results = results.append(total)
results.to_csv("vellamo_metal.csv")


# ## Multi

# In[ ]:


results = pd.DataFrame()
pm_scores = [m for m in pm_df.metric.unique().tolist() if m.startswith('multi')]
total = None
for metric in pm_scores:
    plot,result = collector.report(workload='vellamo', metric=metric,
                     sort_on='mean', ascending=False)
    logging.info("Results for: %s", metric)
    results = results.append(result)
    
    result.rename(columns={metric: 'multi'}, inplace=True)
    if total is None:
        total = result
    else:
        total += result

logging.info("Results for: multi")
results = results.append(total)
results.to_csv("vellamo_multi.csv")


# ## Power

# In[ ]:


for test in collector.tests(workload='vellamo'):
    logging.info("Results for: %s", test)
    plot,result = collector.report(workload='vellamo', metric='VDD_total_energy',
                     test=test, sort_on='mean', ascending=True)
    results_nrj = results_nrj.append(result)


# In[ ]:


for test in collector.tests(workload='vellamo'):
    logging.info("Results for: %s", test)
    plot,result = collector.report(workload='vellamo', metric='VDD_average_power',
                     test=test, sort_on='mean', ascending=True)
    results_nrj = results_nrj.append(result)


# # PCMark Scores

# In[ ]:


pm_df = df[df.workload == 'pcmark']
pm_scores = [m for m in pm_df.metric.unique().tolist() if m.startswith('pcmark_')]
results = pd.DataFrame()


# ## Overall Scores

# In[ ]:


plot,result = collector.report(workload='pcmark', metric='pcmark_Workv2',
                 sort_on='mean', ascending=False);
results = results.append(result)


# ## Individual Tests Scores

# In[ ]:


for metric in pm_scores:
    if metric == 'pcmark_Workv2':
        continue
    plot,result = collector.report(workload='pcmark', metric=metric,
                     sort_on='mean', ascending=False)
    results = results.append(result)
results.to_csv("pcmark.csv")


# In[ ]:


plot,result = collector.report(workload='pcmark', metric='VDD_total_energy',
                     test=test, sort_on='mean', ascending=True)
results_nrj.to_csv("tests_nrj.csv")


# In[ ]:


plot,result = collector.report(workload='pcmark', metric='VDD_average_power',
                     test=test, sort_on='mean', ascending=True)
results_nrj.to_csv("tests_nrj.csv")


# # Generic comparison plots
# `plot_comparisons` can be used to automatically discover metrics that changed between different kernel versions or tags. 

# In[ ]:


logging.info("Here is the list of kernels available:")
logging.info("  %s", ', '.join(df['kernel'].unique().tolist() ))


# In[ ]:


# Select the baseline kernels for comparisions:
# by deafult we use the first available:
kernel_baseline = df['kernel'].iloc[0]
# Or defined here below one of the above reported kernels as baseline for comparisions
#kernel_baseline = "wa"

logging.info("Comparing against baseline kernel: %s", kernel_baseline)
collector.plot_comparisons(base_id=kernel_baseline, by='kernel')

