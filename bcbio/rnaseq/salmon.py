"""
Wrapper for Salmon:
https://github.com/COMBINE-lab/salmon
http://biorxiv.org/content/early/2015/06/27/021592
"""

import os

from bcbio.rnaseq import sailfish
import bcbio.pipeline.datadict as dd
from bcbio.utils import (file_exists, safe_makedir)
from bcbio.distributed.transaction import file_transaction
from bcbio.provenance import do
from bcbio.pipeline import config_utils
from bcbio import bam

def run_salmon_bam(data):
    samplename = dd.get_sample_name(data)
    work_dir = dd.get_work_dir(data)
    salmon_dir = os.path.join(work_dir, "salmon", samplename)
    gtf_file = dd.get_gtf_file(data)
    bam_file = dd.get_transcriptome_bam(data)
    assert file_exists(gtf_file), "%s was not found, exiting." % gtf_file
    fasta_file = dd.get_ref_file(data)
    assert file_exists(fasta_file), "%s was not found, exiting." % fasta_file
    out_file = salmon_quant_bam(bam_file, salmon_dir, gtf_file, fasta_file, data)
    data = dd.set_salmon(data, out_file)
    data = dd.set_salmon_dir(data, salmon_dir)
    return [[data]]

def salmon_quant_bam(bam_file, salmon_dir, gtf_file, ref_file, data):
    safe_makedir(salmon_dir)
    samplename = dd.get_sample_name(data)
    out_file = os.path.join(salmon_dir, "quant.sf")
    if file_exists(out_file):
        return out_file
    gtf_fa = sailfish._create_combined_fasta(data, salmon_dir)
    num_cores = dd.get_num_cores(data)
    strandedness = dd.get_strandedness(data).lower()
    salmon = config_utils.get_program("salmon", dd.get_config(data))
    libtype = _libtype_string(bam_file, strandedness)
    num_cores = dd.get_num_cores(data)
    cmd = ("{salmon} quant {libtype} -p {num_cores} -t {gtf_fa} "
           "-o {tx_out_dir} -a {bam_file} ")
    with file_transaction(data, salmon_dir) as tx_out_dir:
        message = "Quantifying transcripts in %s with Salmon." % bam_file
        do.run(cmd.format(**locals()), message, None)
    return out_file

def _libtype_string(bam_file, strandedness):
    libtype = "-l I" if bam.is_paired(bam_file) else "-l "
    strand = sailfish._sailfish_strand_string(strandedness)
    return libtype + strand

def salmon_index(gtf_file, ref_file, data, out_dir):
    out_dir = os.path.join(out_dir, "index", dd.get_genome_build(data))
    if dd.get_disambiguate(data):
        out_dir = "-".join([out_dir] + dd.get_disambguate(data))
    salmon = config_utils.get_program("salmon", dd.get_config(data))
    num_cores = dd.get_num_cores(data)
    gtf_fa = sailfish._create_combined_fasta(data, out_dir)
    tmpdir = dd.get_tmp_dir(data)
    ### TODO PUT MEMOZATION HERE
    with file_transaction(out_dir) as tx_out_dir:
        cmd = "{salmon} index -k 31 -p {num_cores} -i {tx_out_dir} -t {gtf_fa}"
        message = "Creating Salmon index for {gtf_fa}."
        do.run(cmd.format(**locals()), message.format(**locals()), None)
    return out_dir