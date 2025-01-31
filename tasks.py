import pandas as pd
from pathlib import Path
import numpy as np
from scipy.stats import ttest_ind as ttest
import requests
import tarfile


EXPRESSION_URL = 'https://media.githubusercontent.com/media/cBioPortal/datahub/master/public/hnsc_tcga_pan_can_atlas_2018/data_mrna_seq_v2_rsem.txt'
PHENOTYPE_URL = 'https://media.githubusercontent.com/media/cBioPortal/datahub/master/public/hnsc_tcga_pan_can_atlas_2018/data_clinical_patient.txt'
GENOTYPE_URL = 'https://media.githubusercontent.com/media/cBioPortal/datahub/master/public/hnsc_tcga_pan_can_atlas_2018/data_mutations.txt'
FIREBROWSER_URL = "https://gdac.broadinstitute.org/runs/stddata__2016_01_28/data/HNSC/20160128/gdac.broadinstitute.org_HNSC.Merge_Clinical.Level_1.2016012800.0.0.tar.gz"
MUTATED_GENES = Path('Mutated_Genes.txt')


def download_rawrnaseq(product):
    rawrnaseq = pd.read_table(EXPRESSION_URL)
    Path(str(product)).parent.mkdir(exist_ok=True, parents=True)
    rawrnaseq.to_parquet(str(product))

def download_clinical(product):
    clinical_from_cbioportal = pd.read_table(PHENOTYPE_URL)

    r = requests.get(FIREBROWSER_URL, stream=True)
    if r.status_code == 200:
        with open('/tmp/gdac_broad_clinical.tar.gz', 'wb') as f:
            f.write(r.raw.read())

    t = tarfile.open('/tmp/gdac_broad_clinical.tar.gz', "r")
    with tarfile.open('/tmp/gdac_broad_clinical.tar.gz') as f:
        
        import os
        
        def is_within_directory(directory, target):
            
            abs_directory = os.path.abspath(directory)
            abs_target = os.path.abspath(target)
        
            prefix = os.path.commonprefix([abs_directory, abs_target])
            
            return prefix == abs_directory
        
        def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
        
            for member in tar.getmembers():
                member_path = os.path.join(path, member.name)
                if not is_within_directory(path, member_path):
                    raise Exception("Attempted Path Traversal in Tar File")
        
            tar.extractall(path, members, numeric_owner=numeric_owner) 
            
        
        safe_extract(f, "/tmp")

    clinical_from_broad = pd.read_csv('/tmp/' + FIREBROWSER_URL.split('/')[-1].split('.tar.gz')[0] + '/HNSC.clin.merged.txt', delimiter='\t')
    clinical_from_broad.columns = clinical_from_broad.iloc[0]
    clinical_from_broad = clinical_from_broad[1:]
    clinical_from_broad = clinical_from_broad.T
    clinical_from_broad.columns = clinical_from_broad.loc['admin.bcr']
    clinical_from_broad = clinical_from_broad.drop('admin.bcr')
    clinical_from_broad = clinical_from_broad.set_index('patient.bcr_patient_barcode')
    clinical_from_broad.index.name = None
    clinical_from_broad.index = clinical_from_broad.index.str.upper()

    clinical_from_broad.to_parquet(str(product['clinical_from_broad']))
    clinical_from_cbioportal.to_parquet(str(product['clinical_from_cbioportal']))



def download_mutations(product):
    mutations = pd.read_table(GENOTYPE_URL)
    mutations.to_parquet(str(product))


def generate_traits(upstream, product):

    clinical_from_broad = pd.read_parquet(str(upstream['download_clinical']['clinical_from_broad']))
    clinical_from_cbioportal = pd.read_parquet(str(upstream['download_clinical']['clinical_from_cbioportal']))

    clinical_from_cbioportal = clinical_from_cbioportal.drop([0, 1, 2, 3])
    clinical_from_cbioportal = clinical_from_cbioportal.set_index('#Patient Identifier')
    traits = clinical_from_cbioportal[['Subtype', 'Overall Survival Status']].copy()
    traits.index.name = None
    traits.columns = ['hpv', 'survival']
    traits = traits.dropna()
    traits.hpv = traits.hpv.replace({'HNSC_HPV-': 0, 'HNSC_HPV+': 1})
    traits['survival'] = traits['survival'].replace({'0:LIVING': 1, '1:DECEASED': 0})

    traits = traits.join(clinical_from_broad['patient.tobacco_smoking_history']).dropna()
    traits.columns = ['hpv', 'survival', 'smoker']
    traits.smoker = traits.smoker.apply(lambda x: 0 if x in ['1', '3'] else 1)

    traits.to_parquet(str(product))



def filter_rnaseq(upstream, product):

    rnaseq = pd.read_parquet(str(upstream['download_rawrnaseq']))
    traits = pd.read_parquet(str(upstream['generate_traits']))
    rnaseq.index = rnaseq['Hugo_Symbol']
    rnaseq = rnaseq.drop(['Hugo_Symbol', 'Entrez_Gene_Id'], axis=1)
    rnaseq.columns = rnaseq.columns.str[:-3]
    rnaseq = rnaseq[rnaseq.index.notnull()].dropna().drop_duplicates()
    rnaseq = rnaseq[~rnaseq.index.duplicated(keep='first')]
    rnaseq = rnaseq.loc[:,~rnaseq.columns.duplicated()]
    rnaseq.index.name = None

    rnaseq = rnaseq[rnaseq.mean(1) > 100]
    rnaseq = np.log2(rnaseq + 1)
    rnaseq = rnaseq[~(rnaseq.var(1) < 0.1)]

    def _compare(gene, df=rnaseq):
        hpv_p = df.loc[gene][traits[traits.hpv == 1].index]
        hpv_n = df.loc[gene][traits[traits.hpv == 0].index]
        pvalue = ttest(hpv_p, hpv_n).pvalue

        return pvalue

    pvals = [_compare(gene) for gene in rnaseq.index]

    top_diff_expr_genes = 5000
    rnaseq = rnaseq.loc[pd.DataFrame(pvals, index=rnaseq.index).sort_values(by=0)[:top_diff_expr_genes].index]
    rnaseq.to_parquet(str(product))


def create_mutation_matrix(upstream, product):

    mutations = pd.read_parquet(str(upstream['download_mutations']))
    mutations = mutations[(mutations['Variant_Classification'] != 'Silent')]
    mutations = mutations[(mutations['IMPACT'] != 'LOW')]
    mutations['Barcode'] = mutations['Tumor_Sample_Barcode'].str[:-3]

    mutated_genes = pd.read_csv(MUTATED_GENES, delimiter='\t')
    mutated_genes['Freq'] = mutated_genes['Freq'].str[:-1].astype(float)

    traits = pd.read_parquet(str(upstream['generate_traits']))
    mutation_matrix = pd.DataFrame(columns=set(mutations['Hugo_Symbol']), index = traits.index).fillna(0)
    for patient_id in traits.index:
        for m in set(mutations[mutations.Barcode == patient_id]['Hugo_Symbol']):
            mutation_matrix.loc[patient_id, m] = 1

    mutation_matrix = (mutation_matrix[
            set(mutated_genes
                    .query('Freq > 5')
                    .sort_values(by='Freq', ascending=False).Gene) & 
            set(mutations['Hugo_Symbol'])])

    mutation_matrix.to_parquet(product)


def shape_inputs(upstream, product):
    traits = pd.read_parquet(str(upstream['generate_traits']))
    mutation_matrix = pd.read_parquet(str(upstream['create_mutation_matrix']))
    rnaseq = pd.read_parquet(str(upstream['filter_rnaseq']))

    common_samples = set(rnaseq.columns) & set(mutation_matrix.index) & set(traits.index)
    rnaseq = rnaseq[common_samples].astype(float)
    mutation_matrix = mutation_matrix.loc[common_samples].astype(int)
    traits = traits.loc[common_samples].astype(int)
    rnaseq = rnaseq.subtract(rnaseq.mean(1), 0).div(rnaseq.std(1), 0)

    Z = traits.to_numpy()
    Y = rnaseq.T.to_numpy()
    X = mutation_matrix.to_numpy()

    r = Z.shape[1]
    n = X.shape[0]
    q = Y.shape[1]
    p = X.shape[1]
    assert X.shape[0]==Z.shape[0]
    assert Y.shape[0]<=n

    np.savetxt(product['traits'], Z, delimiter='\t', fmt='%s')
    np.savetxt(product['expression'], Y, delimiter='\t', fmt='%s')
    np.savetxt(product['genotype'], X, delimiter='\t', fmt='%s')

    traits.to_csv(product['traits_csv'])
    mutation_matrix.to_csv(product['mutations_csv'])
    rnaseq.to_csv(product['rnaseq_csv'])