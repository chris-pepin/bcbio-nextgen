"""Subset the genome into standard sets of regions surrounding transcripts.

Provides a central place to bin the genome into smaller transcript-based regions
for structural variant calling and prioritization.
"""
import itertools
import math
import os

import numpy as np
import pybedtools
import toolz as tz

from bcbio import utils
from bcbio.bam import ref
from bcbio.distributed.transaction import file_transaction
from bcbio.pipeline import datadict as dd
from bcbio.provenance import do
from bcbio.variation import bedutils, multi

def calculate_sv_bins(*items):
    """Determine bin sizes and regions to use for samples.

    Unified approach to prepare regional bins for coverage calculations across
    multiple CNV callers. Splits into target and antitarget regions allowing
    callers to take advantage of both. Provides consistent target/anti-target
    bin sizes across batches.

    Uses callable_regions as the access BED file and mosdepth regions in
    variant_regions to estimate depth for bin sizes.
    """
    from bcbio.structural import cnvkit
    if all(not cnvkit.use_general_sv_bins(utils.to_single_data(x)) for x in items):
        return items
    items = [utils.to_single_data(x) for x in items]
    out = []
    for batch, batch_items in multi.group_by_batch(items, False).items():
        work_dir = utils.safe_makedir(os.path.join(dd.get_work_dir(items[0]), "structural", "bins", batch))
        access_file = tz.get_in(["config", "algorithm", "callable_regions"], batch_items[0])
        cnv_file = get_base_cnv_regions(batch_items[0], work_dir, "transcripts100", include_gene_names=False)
        target_bin, anti_bin = _get_target_antitarget_bin_sizes(cnv_file, items)
        for data in batch_items:
            target_bed, anti_bed = cnvkit.targets_w_bins(cnv_file, access_file, target_bin, anti_bin,
                                                         work_dir, data)
            if not data.get("regions"):
                data["regions"] = {}
            data["regions"]["bins"] = {"target": target_bed, "antitarget": anti_bed}
            out.append([data])
    if not len(out) == len(items):
        raise AssertionError("Inconsistent samples in and out of SV bin calculation:\nout: %s\nin : %s" %
                             (sorted([dd.get_sample_name(utils.to_single_data(x)) for x in out]),
                              sorted([dd.get_sample_name(x) for x in items])))
    return out

def _get_target_antitarget_bin_sizes(cnv_file, items):
    """Retrieve target and antitarget bin sizes based on depth.

    Similar to CNVkit's do_autobin but tries to have a standard set of
    ranges (50bp intervals for target and 10kb intervals for antitarget).
    """
    bp_per_bin = 100000  # same target as CNVkit
    range_map = {"target": (100, 250), "antitarget": (10000, 1000000)}
    target_bps = []
    anti_bps = []
    for data in items:
        region_bed = tz.get_in(["depth", "variant_regions", "regions"], data)
        if region_bed:
            for r in pybedtools.BedTool(region_bed).intersect(cnv_file):
                if r.stop - r.start > range_map["target"][0]:
                    target_bps.append(float(r.name))
            for r in pybedtools.BedTool(region_bed).intersect(cnv_file, v=True):
                if r.stop - r.start > range_map["target"][1]:
                    anti_bps.append(float(r.name))
    def scale_in_boundary(raw, round_interval, (min_val, max_val)):
        out = int(math.ceil(raw / float(round_interval)) * round_interval)
        if out > max_val:
            return max_val
        elif out < min_val:
            return min_val
        else:
            return out
    if target_bps:
        raw_target_bin = bp_per_bin / float(np.median(target_bps))
        target_bin = scale_in_boundary(raw_target_bin, 50, range_map["target"])
    else:
        target_bin = range_map["target"][1]

    if anti_bps:
        raw_anti_bin = bp_per_bin / float(np.median(anti_bps))
        anti_bin = scale_in_boundary(raw_anti_bin, 10000, range_map["antitarget"])
    else:
        anti_bin = range_map["antitarget"][1]
    return target_bin, anti_bin

def calculate_sv_coverage(data):
    """Calculate coverage within bins for downstream CNV calling.

    Creates corrected cnr files with log2 ratios and depths.
    """
    from bcbio.variation import coverage
    from bcbio.structural import annotate, cnvkit
    data = utils.to_single_data(data)
    if not cnvkit.use_general_sv_bins(data):
        return [[data]]
    work_dir = utils.safe_makedir(os.path.join(dd.get_work_dir(data), "structural",
                                               dd.get_sample_name(data), "bins"))
    out_target_file = os.path.join(work_dir, "%s-target-coverage.cnn" % dd.get_sample_name(data))
    out_anti_file = os.path.join(work_dir, "%s-antitarget-coverage.cnn" % dd.get_sample_name(data))
    if ((not utils.file_exists(out_target_file) or not utils.file_exists(out_anti_file))
          and (dd.get_align_bam(data) or dd.get_work_bam(data))):
        # mosdepth
        target_cov = coverage.run_mosdepth(data, "target", tz.get_in(["regions", "bins", "target"], data))
        anti_cov = coverage.run_mosdepth(data, "antitarget", tz.get_in(["regions", "bins", "antitarget"], data))
        target_cov_genes = annotate.add_genes(target_cov.regions, data, max_distance=0)
        anti_cov_genes = annotate.add_genes(anti_cov.regions, data, max_distance=0)
        out_target_file = _add_log2_depth(target_cov_genes, out_target_file, data)
        out_anti_file = _add_log2_depth(anti_cov_genes, out_anti_file, data)
        # TODO: Correct for GC bias
    if os.path.exists(out_target_file):
        data["depth"]["bins"] = {"target": out_target_file, "antitarget": out_anti_file}
    return [[data]]

def _add_log2_depth(in_file, out_file, data):
    """Create a CNVkit cnn file with depths
    http://cnvkit.readthedocs.io/en/stable/fileformats.html?highlight=cnn#target-and-antitarget-bin-level-coverages-cnn
    """
    if not utils.file_exists(out_file):
        with file_transaction(data, out_file) as tx_out_file:
            with utils.open_gzipsafe(in_file) as in_handle:
                with open(tx_out_file, "w") as out_handle:
                    out_handle.write("chromosome\tstart\tend\tgene\tlog2\tdepth\n")
                    for line in in_handle:
                        parts = line.rstrip().split()
                        if len(parts) > 4:
                            chrom, start, end, orig_name, depth, gene_name = parts
                            depth = float(depth)
                            log2_depth = math.log(float(depth), 2) if depth else -20.0
                            out_handle.write("%s\t%s\t%s\t%s\t%.3f\t%.2f\n" %
                                             (chrom, start, end, gene_name, log2_depth, depth))
    return out_file

# Region retrieval for SV calling

def get_base_cnv_regions(data, work_dir, genome_default="transcripts1e4", include_gene_names=True):
    """Retrieve set of target regions for CNV analysis.

    Subsets to extended transcript regions for WGS experiments to avoid
    long runtimes.
    """
    cov_interval = dd.get_coverage_interval(data)
    base_regions = get_sv_bed(data, include_gene_names=include_gene_names)
    # if we don't have a configured BED or regions to use for SV caling
    if not base_regions:
        # For genome calls, subset to regions near genes as targets
        if cov_interval == "genome":
            base_regions = get_sv_bed(data, genome_default, work_dir, include_gene_names=include_gene_names)
            if base_regions:
                base_regions = remove_exclude_regions(base_regions, base_regions, [data])
        # Finally, default to the defined variant regions
        if not base_regions:
            base_regions = dd.get_variant_regions(data)
    return bedutils.clean_file(base_regions, data)

def remove_exclude_regions(orig_bed, base_file, items, remove_entire_feature=False):
    """Remove centromere and short end regions from an existing BED file of regions to target.
    """
    from bcbio.structural import shared as sshared
    out_bed = os.path.join("%s-noexclude.bed" % (utils.splitext_plus(base_file)[0]))
    if not utils.file_uptodate(out_bed, orig_bed):
        exclude_bed = sshared.prepare_exclude_file(items, base_file)
        with file_transaction(items[0], out_bed) as tx_out_bed:
            pybedtools.BedTool(orig_bed).subtract(pybedtools.BedTool(exclude_bed),
                                                  A=remove_entire_feature, nonamecheck=True).saveas(tx_out_bed)
    if utils.file_exists(out_bed):
        return out_bed
    else:
        return orig_bed

def get_sv_bed(data, method=None, out_dir=None, include_gene_names=True):
    """Retrieve a BED file of regions for SV and heterogeneity calling using the provided method.

    method choices:
      - exons: Raw BED file of exon regions
      - transcripts: Full collapsed regions with the min and max of each transcript.
      - transcriptsXXXX: Collapsed regions around transcripts with a window size of
        XXXX.
      - A custom BED file of regions
    """
    if method is None:
        method = tz.get_in(["config", "algorithm", "sv_regions"], data)
    gene_file = dd.get_gene_bed(data)
    if method and os.path.isfile(method):
        return method
    elif not gene_file or not method:
        return None
    elif method == "exons":
        return gene_file
    elif method.startswith("transcripts"):
        window = method.split("transcripts")[-1]
        window = int(float(window)) if window else 0
        return _collapse_transcripts(gene_file, window, data, out_dir, include_gene_names=include_gene_names)
    else:
        raise ValueError("Unexpected transcript retrieval method: %s" % method)

def _collapse_transcripts(in_file, window, data, out_dir, include_gene_names=True):
    """Collapse transcripts into min/max coordinates and optionally add windows.
    """
    if out_dir is None:
        out_dir = os.path.dirname(in_file)
    out_file = os.path.join(out_dir,
                            "%s-transcripts_w%s.bed" % (os.path.splitext(os.path.basename(in_file))[0],
                                                        window))
    chrom_sizes = {}
    for contig in ref.file_contigs(dd.get_ref_file(data), data["config"]):
        chrom_sizes[contig.name] = contig.size
    if not utils.file_uptodate(out_file, in_file):
        with file_transaction(data, out_file) as tx_out_file:
            prep_file = "%s-sortprep%s" % os.path.splitext(tx_out_file)
            sort_cmd = bedutils.get_sort_cmd()
            cmd = "{sort_cmd} -k4,4 -k1,1 {in_file} > {prep_file}"
            do.run(cmd.format(**locals()), "Sort BED file by transcript name")
            with open(tx_out_file, "w") as out_handle:
                # Work around for segmentation fault issue with groupby
                # https://github.com/daler/pybedtools/issues/131#issuecomment-89832476
                x = pybedtools.BedTool(prep_file)
                def gen():
                    for r in x:
                        yield r
                for name, rs in itertools.groupby(gen(), lambda r: (r.name, r.chrom)):
                    rs = list(rs)
                    r = rs[0]
                    for gcoords in _group_coords(rs):
                        min_pos = max(min(gcoords) - window, 0)
                        max_pos = min(max(gcoords) + window, chrom_sizes[r.chrom])
                        if include_gene_names:
                            out_handle.write("%s\t%s\t%s\t%s\n" % (r.chrom, min_pos, max_pos, r.name))
                        else:
                            out_handle.write("%s\t%s\t%s\n" % (r.chrom, min_pos, max_pos))
    return bedutils.sort_merge(out_file, data)

def _group_coords(rs):
    """Organize coordinate regions into groups for each transcript.

    Avoids collapsing very large introns or repetitive genes spread across
    the chromosome by limiting the intron size to 100kb for creating a single transcript
    """
    max_intron_size = 1e5
    coords = []
    for r in rs:
        coords.append(r.start)
        coords.append(r.end)
    coord_groups = []
    cur_group = []
    for coord in sorted(coords):
        if not cur_group or coord - cur_group[-1] < max_intron_size:
            cur_group.append(coord)
        else:
            coord_groups.append(cur_group)
            cur_group = [coord]
    if cur_group:
        coord_groups.append(cur_group)
    return coord_groups
