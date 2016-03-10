#!/usr/bin/env python3

import sys
import os
import copy

import numpy as np
import scipy as sp
import scipy.stats

import camoco as co
import pandas as pd
import matplotlib.pylab as plt
import statsmodels.api as sm

from camoco.Tools import confidence_interval, \
                         mean_confidence_interval,\
                         NearestDict

lowess = sm.nonparametric.lowess

def locality(args):
    # Generate output dirs
    if args.out != sys.stdout:
        args.out = "{}_Locality.csv".format(args.out.replace('.csv',''))        
    if os.path.dirname(args.out) != '':
        os.makedirs(os.path.dirname(args.out),exist_ok=True)
    if os.path.exists("{}_Locality.csv".format(args.out.replace('.csv',''))):
        print(
            "{}_Locality.csv exists! Skipping!".format(
                args.out.replace('.csv','')
            )
        )
        return None
    # Grab the COB object
    cob = co.COB(args.cob)
    gwas = co.GWAS(args.gwas)
    # If there is a different score for 'significant', update the COB object
    if args.sig_edge_zscore is not None:
        cob.set_sig_edge_zscore(args.sig_edge_zscore)
    # If all, grab a generater
    if 'all' in args.terms:
        terms = gwas.iter_terms()
    else:
        # Otherwise get the term out of the GWAS
        terms = [gwas[x] for x in args.terms] 

    all_results = []
    # Add in text for axes
    for term in terms:
        # Generate data using permuted arguments
        loc,bsloc,fdr = generate_data(cob,gwas,term,args) 
        if args.plot:
            fig,(ax1,ax2,ax3) = plt.subplots(1,3,figsize=(24,8))
            # Plot the data
            plot_data(args,loc,bsloc,fdr,ax1)
            plot_scatter(args,loc,bsloc,fdr,ax2)
            plot_fdr(args,loc,bsloc,fdr,ax3)
            plt.tight_layout()
            plt.savefig("{}_{}.png".format(
                args.out,
                term.id
            ))
            plt.close()
            # Keep track of this shit
        all_results.append(loc)
    # Add parameters to the data frame
    all_results = pd.concat(all_results)
    all_results.insert(0,'COB',cob.name)
    all_results.insert(0,'Ontology',gwas.name)
    all_results.insert(0,'WindowSize',args.candidate_window_size)
    all_results.insert(0,'FlankLimit',args.candidate_flank_limit)
    # Output the Locality Measures
    all_results.to_csv(args.out)
        
def generate_data(cob,gwas,term,args):
    '''
        Generates the data according to parameters in args
    '''
    if args.snp2gene == 'effective':
        loci = sorted(term.effective_loci(
            window_size=args.candidate_window_size
        ))
    elif args.snp2gene == 'strongest':
        loci = term.strongest_loci(
            window_size=args.candidate_window_size,
            attr=args.strongest_attr,
            lowest=args.strongest_higher
        )
    else:
        raise ValueError('{} not valid snp2gene mapping'.format(args.snp2gene))
    
    candidate_genes = cob.refgen.candidate_genes(
        loci,
        flank_limit=args.candidate_flank_limit
    )

    # Find the empirical Locality
    loc = cob.locality(
        candidate_genes,
        include_regression=True
    )
    loc.insert(0,'Term',term.id)
    loc['iter_name'] = 'emp' #cringe
   
    # Find the Bootstrapped Locality
    bsloc = pd.concat(
        [cob.locality(
            cob.refgen.bootstrap_candidate_genes(
                loci,
                flank_limit=args.candidate_flank_limit
            ),
            iter_name='bs'+str(x),
            include_regression=False
        ) for x in range(args.num_bootstraps)]
    )
    bsloc.insert(0,'Term',term.id)

    '''---------------------------------------------------
        Empirical and SD Calculations
    '''
    # We need to perform regression for the entire bootstrap dataset
    OLS = sm.OLS(bsloc['local'],bsloc['global']).fit()
    bsloc['fitted'] = OLS.fittedvalues
    bsloc['resid'] = OLS.resid

    # Windowing
    bsloc = bsloc.sort('fitted')
    # Find out how many tick there are with X items per window
    num_windows = len(bsloc) // args.regression_window_size
    window_ticks = len(bsloc) // num_windows
    bsloc['window'] = [int(i/window_ticks) for i in range(len(bsloc))]
    # If there are not many in the last window, change it second to last
    max_window = max(bsloc['window'])
    if sum(bsloc['window'] == max_window) < args.regression_window_size / 2:
        bsloc.loc[bsloc['window'] == max_window, 'window'] = max_window-1
    # create a dictionary so we can map the empirical data later
    win_map = NearestDict({
        # Good god this is a hack -- 
        # group the pandas df by window and calculate the mean fitted value, 
        # create dict from that (via comprehension) and reverse keys and values
        fitted:window for window,fitted in bsloc.groupby('window').apply(
                lambda df: np.max(df['fitted'])
            ).to_dict().items() 
    })
    # create a dict of window to std mapping
    win_std = bsloc.groupby('window').apply(lambda df: df['resid'].std())
    # perform lowess on the std_windows
    win_std = NearestDict(
        win_std.to_dict()
    )
    fit_std = {f:win_std[w]for f,w in win_map.items()}
    
    # Create a dict where keys are fitted values and 
    # values are that fitted values std
    fit_std = NearestDict(
        pd.DataFrame(
            lowess(
                np.array(list(fit_std.values())),
                np.array(list(fit_std.keys())),
                is_sorted=False
            ),columns=['fitted','sd']
        ).sort('fitted').set_index('fitted').to_dict()['sd']
    )

    # Calculate the s.d. of the residuals in each window
    # divide empirical residuals by the s.d. in their respective window
    loc['bs_std'] = [fit_std[x] for x in loc['fitted']]
    loc['zscore'] = [x['resid']/x['bs_std'] for i,x in loc.iterrows()]
    loc = loc.sort('zscore',ascending=False)

    bsloc['bs_std'] = [fit_std[x] for x in bsloc['fitted']]
    bsloc['zscore'] = [x['resid']/x['bs_std'] for i,x in bsloc.iterrows()]
    bsloc = bsloc.sort('zscore',ascending=False)

    '''---------------------------------------------------
        FDR Calculations
    '''
    # Repeat bootstraps to assess global FDR
    fdr = pd.concat(
            [cob.locality(
                cob.refgen.bootstrap_candidate_genes(
                    loci,
                    flank_limit=args.candidate_flank_limit
                ),
                iter_name='fdr'+str(x),
                include_regression=False
            ) for x in range(args.num_bootstraps)]
    ).sort('global')
    fdr.insert(0,'Term',term.id)

    OLS = sm.OLS(fdr['local'],fdr['global']).fit()
    fdr['fitted'] = OLS.fittedvalues
    fdr['resid'] = OLS.resid

    # Remove global degree larger than empirical
    fdr = fdr[fdr['global'] <= max(loc['global'])]
    fdr['window'] = [int(x/window_ticks) for x in fdr['global']]

    # calculate z-scores for the global 
    fdr['bs_std'] = [fit_std[x] for x in fdr['fitted']]
    fdr['zscore'] = [x['resid']/x['bs_std'] for i,x in fdr.iterrows()]
    # Generate the ZScore vales
    if args.gene_specific == True:
        for zscore in range(0,8):
            fdr_indices = fdr[fdr.zscore >= zscore]
            if len(fdr_indices) > 0:
                num_random = fdr_indices.groupby('iter_name').apply(len).mean()
            else:
                num_random = 0
            num_real = sum(loc.zscore >= zscore)
            if num_real != 0 and num_random != 0:
                zfdr = num_random/num_real
            else:
                zfdr = 1
            loc.loc[loc.zscore>=zscore,'fdr'] = zfdr
    else:
        # aggregate locality for each term in loc and fdr
        loc = loc.groupby('Term').apply(np.mean)
        fdr = fdr.groupby('iter_name').apply(np.mean)
        average_resid = loc.ix[term.id].resid
        loc['pval'] = sum(
            [x >= average_resid for x in fdr.resid.values]
        )/len(fdr)

    # Give em the gold
    return loc,bsloc,fdr

def plot_scatter(args,loc,bsloc,fdr,ax):
    ''' ---------------------------------------------------
        Plotting
    '''
    # Y axis is local degree (what we are TRYING to predict)
    ax.set_ylim(0,max(loc['local']))
    ax.set_xlim(0,max(loc['global']))
    ax.set_xlabel('Number Global Interactions')
    ax.set_ylabel('Number Local Interactions')

    # UGH! map lowess 
    fdrlowess = lowess(
        fdr['local'],fdr['global'],
        frac=0.15,it=5,delta=0.1*len(fdr),
        is_sorted=False
    )
    # plot the bootstrap points
    ax.plot(fdr['global'],fdr['local'],'ro',alpha=0.05,label='Bootstraps')
    # plot the OLS lowess line
    ci = fdr.groupby('window')['fitted','global'].agg(
        [np.mean,confidence_interval]
    )
    
    #for win,df in fdr.groupby('iter_name'):
    #    ax.plot(df['global'],df['fitted'],alpha=1)
        
    ax.errorbar(
        ci['global','mean'],ci['fitted','mean'],
        yerr=ci['fitted','confidence_interval'],
        color='g',label='Bootstrap OLS'
    )

    #plot the empirical data
    ax.plot(loc['global'],loc['local'],'bo',alpha=1,label='Empirical')
    ax.plot(loc['global'],loc['fitted'],'k-',alpha=1,label='Empirical OLS')
    # finish plots
    #legend = ax.legend(loc='best') 

def plot_fdr(args,loc,bsloc,fdr,ax):
    '''---------------------------------------------------
        FDR Plotting
    '''
    # Plot the empirical Z-score distributions
    zscores = list(np.arange(1,8,0.5))
    zloc = [
        sum(np.logical_and(
            loc['zscore'] >= x ,
            loc['local'] >= args.min_fdr_degree
        )) 
        for x in zscores
    ]
    ax.plot(zscores,zloc,'bo',label='Empirical Zscore > x')
    # plot the fdr scores spread
    zcdf = pd.DataFrame(
        [ mean_confidence_interval(
            fdr.groupby('iter_name').apply(
                lambda df: sum(np.logical_and(
                    df['zscore'] >= x,
                    df['local'] >= args.min_fdr_degree 
                ))
            )
        ) for x in zscores ],
        columns=['mean','ci']
    )
    ax.errorbar(
        zscores,
        zcdf['mean'],
        yerr=zcdf['ci'],
        label='Bootstrap Z-scores',
        color='red'
    )  
    ax.set_xlabel('Z-Score')
    ax.set_ylabel('Number of Genes > Z')
    ax.set_title('Z Score FDR')

def plot_data(args,loc,bsloc,fdr,ax):
    ax.xaxis.set_visible(False)
    ax.yaxis.set_visible(False)
    ax.text(0,0,'''
        COB: {}
        Ontology: {}
        Term: {}
    '''.format(
        args.cob,
        args.gwas,
        args.terms
    ))