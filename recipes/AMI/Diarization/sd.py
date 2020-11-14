#!/usr/bin/python
"""
This recipe implements diarization baseline
using deep embedding extraction followed by spectral clustering.
We use nearest-neighbor based affinity matrix.

Condition: Oracle VAD and Oracle number of speakers.

Note: There are multiple ways to write this recipe. We chose to iterate over individual files.
This method is less GPU memory demanding and also makes code easy to understand.
"""

import os
import sys
import torch
import logging
import speechbrain as sb
import numpy as np
import pickle
import csv
import glob
import shutil
import warnings
import time
import diarization as diar
from tqdm.contrib import tqdm

from scipy.sparse.linalg import eigsh
from scipy.sparse.csgraph import laplacian as csgraph_laplacian

from speechbrain.utils.data_utils import download_file
from speechbrain.data_io.data_io import DataLoaderFactory
from speechbrain.processing.PLDA_LDA import StatObject_SB
from speechbrain.utils.DER import DER

# from diarization import *  # noqa F403

np.random.seed(1234)

# Logger setup
logger = logging.getLogger(__name__)
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(current_dir))

from ami_prepare import prepare_ami  # noqa E402

try:
    from sklearn.neighbors import kneighbors_graph
    from sklearn.cluster import SpectralClustering
    from sklearn.cluster._kmeans import k_means
except ImportError:
    err_msg = "The optional dependency sklearn is used in this module\n"
    err_msg += "Cannot import sklearn. \n"
    err_msg += "Please follow the below instructions\n"
    err_msg += "=============================\n"
    err_msg += "Using pip:\n"
    err_msg += "pip install sklearn\n"
    err_msg += "================================ \n"
    err_msg += "Using conda:\n"
    err_msg += "conda install sklearn"
    raise ImportError(err_msg)


def compute_embeddings(wavs, lens):
    """Definition of the steps for computation of embeddings from the waveforms
    """
    with torch.no_grad():
        wavs = wavs.to(params["device"])
        feats = params["compute_features"](wavs)
        feats = params["mean_var_norm"](feats, lens)
        emb = params["embedding_model"](feats, lens=lens)
        emb = params["mean_var_norm_emb"](
            emb, torch.ones(emb.shape[0], device=params["device"])
        )

    return emb


def download_and_pretrain():
    """Downloads pre-trained model
    """
    save_model_path = params["model_dir"] + "/emb.ckpt"
    download_file(params["embedding_file"], save_model_path)
    params["embedding_model"].load_state_dict(
        torch.load(save_model_path), strict=True
    )


# I think this should be in the recipe
def embedding_computation_loop(split, set_loader, stat_file):
    """Extracts embeddings for a given dataset loader
    """

    # Extract embeddings (skip if already done)
    if not os.path.isfile(stat_file):

        embeddings = np.empty(shape=[0, params["emb_dim"]], dtype=np.float64)
        modelset = []
        segset = []
        with tqdm(set_loader, dynamic_ncols=True) as t:
            # different data may have different statistics
            params["mean_var_norm_emb"].count = 0

            for wav in t:
                ids, wavs, lens = wav[0]

                mod = [x for x in ids]
                seg = [x for x in ids]
                modelset = modelset + mod
                segset = segset + seg

                # embedding computation
                emb = compute_embeddings(wavs, lens).squeeze(1).cpu().numpy()
                embeddings = np.concatenate((embeddings, emb), axis=0)

        modelset = np.array(modelset, dtype="|O")
        segset = np.array(segset, dtype="|O")

        # Intialize variables for start, stop and stat0
        s = np.array([None] * embeddings.shape[0])
        b = np.array([[1.0]] * embeddings.shape[0])

        stat_obj = StatObject_SB(
            modelset=modelset,
            segset=segset,
            start=s,
            stop=s,
            stat0=b,
            stat1=embeddings,
        )
        logger.info(f"Saving Embeddings...")
        stat_obj.save_stat_object(stat_file)

    else:
        logger.info(f"Skipping embedding extraction (as already present)")
        logger.info(f"Loading previously saved embeddings")

        with open(stat_file, "rb") as in_file:
            stat_obj = pickle.load(in_file)

    return stat_obj


################################################


def spectral_embedding_sb(
    adjacency, n_components=8, norm_laplacian=True, drop_first=True,
):

    # random_state = check_random_state(random_state)

    # Whether to drop the first eigenvector
    if drop_first:
        n_components = n_components + 1

    if not diar.graph_is_connected(adjacency):
        warnings.warn(
            "Graph is not fully connected, spectral embedding"
            " may not work as expected."
        )

    laplacian, dd = csgraph_laplacian(
        adjacency, normed=norm_laplacian, return_diag=True
    )

    laplacian = diar.set_diag(laplacian, 1, norm_laplacian)

    laplacian *= -1
    # v0 = random_state.uniform(-1, 1, laplacian.shape[0])

    # vals, diffusion_map = eigsh(
    #    laplacian, k=n_components, sigma=1.0, which="LM", tol=eigen_tol, v0=v0
    # )

    vals, diffusion_map = eigsh(
        laplacian, k=n_components, sigma=1.0, which="LM",
    )

    embedding = diffusion_map.T[n_components::-1]

    if norm_laplacian:
        embedding = embedding / dd

    embedding = diar.deterministic_vector_sign_flip(embedding)
    if drop_first:
        return embedding[1:n_components].T
    else:
        return embedding[:n_components].T


def spectral_clustering_sb(
    affinity,
    n_clusters=8,
    n_components=None,
    eigen_solver=None,
    random_state=None,
    n_init=10,
    eigen_tol=0.0,
    assign_labels="kmeans",
):

    random_state = diar.check_random_state(random_state)
    n_components = n_clusters if n_components is None else n_components

    maps = spectral_embedding_sb(
        affinity, n_components=n_components, drop_first=False,
    )

    _, labels, _ = k_means(
        maps, n_clusters, random_state=random_state, n_init=n_init
    )

    return labels


def do_spec_clustering(diary_obj_eval, out_rttm_file, rec_id, k=4):
    """Performs spectral clustering on embeddings
    """
    clust_obj = Spec_Cluster(
        n_clusters=k,
        assign_labels="kmeans",
        random_state=params["seed"],
        affinity="nearest_neighbors",
    )

    clust_obj.perform_sc(diary_obj_eval.stat1)

    labels = clust_obj.labels_

    # Convert labels to speaker boundaries
    subseg_ids = diary_obj_eval.segset
    lol = []

    for i in range(labels.shape[0]):
        spkr_id = rec_id + "_" + str(labels[i])

        sub_seg = subseg_ids[i]

        splitted = sub_seg.rsplit("_", 2)
        rec_id = str(splitted[0])
        sseg_start = float(splitted[1])
        sseg_end = float(splitted[2])

        a = [rec_id, sseg_start, sseg_end, spkr_id]
        lol.append(a)

    # Sorting based on start time of sub-segment
    lol.sort(key=lambda x: float(x[1]))

    # Merge and split in 2 simple steps: (i) Merge sseg of same speakers then (ii) split different speakers
    # Step 1: Merge adjacent sub-segments that belong to same speaker (or cluster)
    lol = diar.merge_ssegs_same_speaker(lol)

    # Step 2: Distribute duration of adjacent overlapping sub-segments belonging to different speakers (or cluster)
    # Taking mid-point as the splitting time location.
    lol = diar.distribute_overlap(lol)

    logger.info("Completed diarizing " + rec_id)
    diar.write_rttm(lol, out_rttm_file)


class Spec_Cluster(SpectralClustering):
    def perform_sc(self, X):
        """Performs spectral clustering using sklearn on embeddings (X).

        Arguments
        ---------
        X : array (n_samples, n_features)
            Embeddings to be clustered
        """

        # Computation of affinity matrix
        connectivity = kneighbors_graph(
            X,
            n_neighbors=params["n_neighbors"],
            include_self=params["include_self"],
        )
        self.affinity_matrix_ = 0.5 * (connectivity + connectivity.T)

        # Perform spectral clustering on affinity matrix
        self.labels_ = spectral_clustering_sb(
            self.affinity_matrix_,
            n_clusters=self.n_clusters,
            assign_labels=self.assign_labels,
        )
        return self


def diarize_dataset(full_csv, split_type, n_lambdas):
    """Diarizes all the recordings in a given dataset
    """

    # Prepare `spkr_info` only once when Oracle num of speakers is selected
    if params["oracle_n_spkrs"] is True:
        full_ref_rttm_file = (
            params["ref_rttm_dir"] + "/fullref_ami_" + split_type + ".rttm"
        )
        RTTM = []
        with open(full_ref_rttm_file, "r") as f:
            for line in f:
                entry = line[:-1]
                RTTM.append(entry)

        spkr_info = list(  # noqa F841
            filter(lambda x: x.startswith("SPKR-INFO"), RTTM)
        )

    # Get all recording IDs in this dataset
    A = [row[0].rstrip().split("_")[0] for row in full_csv]
    all_rec_ids = list(set(A[1:]))
    all_rec_ids.sort()

    N = str(len(all_rec_ids))
    split = "AMI_" + split_type
    i = 1

    # Pretrain model
    if "https://" in params["embedding_file"]:
        download_and_pretrain()
    else:
        params["embedding_model"].load_state_dict(
            torch.load(params["embedding_file"]), strict=True
        )

    # Setting eval modality
    params["embedding_model"].eval()

    for rec_id in all_rec_ids:

        tag = "[" + str(split_type) + ": " + str(i) + "/" + N + "]"
        i = i + 1

        msg = "Diarizing %s : %s " % (tag, rec_id)
        logger.info(msg)

        if not os.path.exists(os.path.join(params["embedding_dir"], split)):
            os.makedirs(os.path.join(params["embedding_dir"], split))

        diary_stat_file = os.path.join(
            params["embedding_dir"], split, rec_id + "_xv_stat.pkl"
        )

        # Prepare a csv for a recording
        new_csv_file = os.path.join(
            params["embedding_dir"], split, rec_id + ".csv"
        )
        diar.prepare_subset_csv(full_csv, rec_id, new_csv_file)

        # Setup a dataloader for above one recording (above csv)
        diary_set = DataLoaderFactory(
            new_csv_file,
            params["diary_loader_eval"].batch_size,
            params["diary_loader_eval"].csv_read,
            params["diary_loader_eval"].sentence_sorting,
        )

        diary_set_loader = diary_set.forward().get_dataloader()

        # Putting modules on the device
        params["compute_features"].to(params["device"])
        params["mean_var_norm"].to(params["device"])
        params["embedding_model"].to(params["device"])
        params["mean_var_norm_emb"].to(params["device"])

        # Compute Embeddings
        diary_obj_dev = embedding_computation_loop(
            "diary", diary_set_loader, diary_stat_file
        )

        # Perform spectral clustering
        out_rttm_dir = os.path.join(params["sys_rttm_dir"], split)
        if not os.path.exists(out_rttm_dir):
            os.makedirs(out_rttm_dir)
        out_rttm_file = out_rttm_dir + "/" + rec_id + ".rttm"

        if params["oracle_n_spkrs"] is True:
            # Oracle num of speakers
            num_spkrs = diar.get_oracle_num_spkrs(rec_id, spkr_info)
        else:
            # Num of speakers tunned on dev set
            num_spkrs = n_lambdas

        do_spec_clustering(diary_obj_dev, out_rttm_file, rec_id, k=num_spkrs)

    # Concatenate individual RTTM files
    # This is not needed but just staying with the standards
    concate_rttm_file = out_rttm_dir + "/sys_output.rttm"

    # logger.info("Concatenating individual RTTM files...")
    with open(concate_rttm_file, "w") as cat_file:
        for f in glob.glob(out_rttm_dir + "/*.rttm"):
            if f == concate_rttm_file:
                continue
            with open(f, "r") as indi_rttm_file:
                shutil.copyfileobj(indi_rttm_file, cat_file)

    msg = "The system generated RTTM file for %s set : %s" % (
        split_type,
        concate_rttm_file,
    )
    logger.info(msg)

    return concate_rttm_file


def dev_tuner(full_csv, split_type):
    """Tuning n_compenents on dev set. (Basic tunning).
    Returns:
        n_lambdas = n_components
    """

    DER_list = []
    for n_lambdas in range(1, params["max_num_spkrs"] + 1):

        # Process whole dataset for value of n_lambdas
        concate_rttm_file = diarize_dataset(full_csv, split_type, n_lambdas)

        ref_rttm = os.path.join(params["ref_rttm_dir"], "fullref_ami_dev.rttm")
        sys_rttm = concate_rttm_file
        [MS, FA, SER, DER_] = DER(
            ref_rttm,
            sys_rttm,
            params["ignore_overlap"],
            params["forgiveness_collar"],
        )

        msg = "[Tuner]: n_lambdas= %d , DER= %s\n" % (
            n_lambdas,
            str(round(DER_, 2)),
        )

        logger.info(msg)
        DER_list.append(DER_)

    # Take n_lambdas with minmum DER
    tuned_n_lambdas = DER_list.index(min(DER_list)) + 1

    return tuned_n_lambdas


# Begin!
if __name__ == "__main__":  # noqa: C901

    # Load hyperparameters file with command-line overrides
    params_file, overrides = sb.core.parse_arguments(sys.argv[1:])

    with open(params_file) as fin:
        params = sb.yaml.load_extended_yaml(fin, overrides)

    # Create experiment directory
    sb.core.create_experiment_directory(
        experiment_directory=params["output_folder"],
        hyperparams_to_save=params_file,
        overrides=overrides,
    )

    # Few more experiment directories (to have cleaner structure)
    exp_dirs = [
        params["model_dir"],
        params["embedding_dir"],
        params["csv_dir"],
        params["ref_rttm_dir"],
        params["sys_rttm_dir"],
    ]
    for dir_ in exp_dirs:
        if not os.path.exists(dir_):
            os.makedirs(dir_)

    # Prepare data for AMI
    logger.info(
        "AMI: Data preparation [Prepares both, the reference RTTMs and the CSVs]"
    )
    prepare_ami(
        data_folder=params["data_folder"],
        manual_annot_folder=params["manual_annot_folder"],
        save_folder=params["save_folder"],
        split_type=params["split_type"],
        skip_TNO=params["skip_TNO"],
        mic_type=params["mic_type"],
        vad_type=params["vad_type"],
        max_subseg_dur=params["max_subseg_dur"],
        overlap=params["overlap"],
    )

    # AMI Dev Set
    full_csv = []
    with open(params["csv_diary_dev"], "r") as csv_file:
        reader = csv.reader(csv_file, delimiter=",")
        for row in reader:
            full_csv.append(row)

    # TUNING for num of lambdas
    if params["oracle_n_spkrs"] is False:
        a = time.time()
        n_lambdas = dev_tuner(full_csv, "dev")
        msg = "Tuning completed! Total time spent in tuning = %s seconds\n" % (
            str(round(time.time() - a, 2))
        )
        logger.info(msg)
    else:
        msg = "Running for Oracle number of speakers"
        logger.info(msg)
        n_lambdas = None  # will be taken from groundtruth

    out_boundaries = diarize_dataset(full_csv, "dev", n_lambdas=n_lambdas)

    # Evaluating on DEV set
    logger.info("Evaluating for AMI Dev. set")
    ref_rttm = os.path.join(params["ref_rttm_dir"], "fullref_ami_dev.rttm")
    sys_rttm = out_boundaries
    [MS_dev, FA_dev, SER_dev, DER_dev] = DER(
        ref_rttm,
        sys_rttm,
        params["ignore_overlap"],
        params["forgiveness_collar"],
    )
    msg = "AMI Dev set: Diarization Error Rate = %s %%\n" % (
        str(round(DER_dev, 2))
    )
    logger.info(msg)

    # AMI Eval Set
    full_csv = []
    with open(params["csv_diary_eval"], "r") as csv_file:
        reader = csv.reader(csv_file, delimiter=",")
        for row in reader:
            full_csv.append(row)

    out_boundaries = diarize_dataset(full_csv, "eval", n_lambdas=n_lambdas)

    # Evaluating on EVAL set
    logger.info("Evaluating for AMI Eval. set")
    ref_rttm = os.path.join(params["ref_rttm_dir"], "fullref_ami_eval.rttm")
    sys_rttm = out_boundaries
    [MS_eval, FA_eval, SER_eval, DER_eval] = DER(
        ref_rttm,
        sys_rttm,
        params["ignore_overlap"],
        params["forgiveness_collar"],
    )
    msg = "AMI Eval set: Diarization Error Rate = %s %%\n" % (
        str(round(DER_eval, 2))
    )
    logger.info(msg)

    msg = (
        "Final Diarization Error Rate (%%) on AMI corpus: Dev = %s %% | Eval = %s %%\n"
        % (str(round(DER_dev, 2)), str(round(DER_eval, 2)))
    )
    logger.info(msg)
