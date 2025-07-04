import argparse
from .do_spreading import *
from .time_spreading import *
from .fft_ear_model import *
from .utils import *
from .group_into_bands import *
from .create_bark import *
from .modulation import *
import soundfile as sf
from soundfile import SoundFile
from .threshold import *
import numpy as np
from .MOV import *
from scipy.io import wavfile
from .wavfile_utils import *
from .neural import *
from tqdm import tqdm


def boundary(ch1ref, ch1test, rate, hann=HANN, BOUNDLIMIT=200, BOUNDWIN=5):
    for k in range(0, hann - BOUNDWIN + 1):
        ch1t = sum(abs(ch1test[k : k + BOUNDWIN])) > BOUNDLIMIT
        ch1r = sum(abs(ch1ref[k : k + BOUNDWIN])) > BOUNDLIMIT

        if ch1t or ch1r:
            return True

    return False


def process_audio_block(
    ch1ref,
    ch1test,
    rate=16000,
    hann=HANN,
    lpref=92,
    lptest=92,
    state={},
    boundflag=0,
    test_rate=16000,
):
    fC, fL, fR = calculate_bark_bands(80, 8000)
    harm_samples = 1
    while harm_samples < (18000 / test_rate) * (HANN / 4):
        harm_samples *= 2

    delaytime1 = np.ceil(THRESHOLDDELAY * test_rate * 2 / HANN)
    delaytime2 = np.ceil(AVERAGINGDELAY * test_rate * 2 / HANN)

    ffteref, fftref = earmodelfft(ch1ref, 1, lpref, hann)
    fftetest, ffttest = earmodelfft(ch1test, 1, lptest, hann)
    ppref = critbandgroup(ffteref, rate, hann=HANN, bark_table=(fC, fL, fR))
    ppref = AddIntNoise(ppref, fC)

    pptest = critbandgroup(fftetest, rate, hann=HANN, bark_table=(fC, fL, fR))
    pptest = AddIntNoise(pptest, fC)

    fnoise = np.abs(ffteref) - np.abs(fftetest)
    ppnoise = critbandgroup(fnoise, rate, hann, bark_table=(fC, fL, fR))

    E2test = spreading(pptest, fC)
    E2ref = spreading(ppref, fC)

    Etest, Etmptest = time_spreading(E2test, rate, fC)
    Eref, Etmpref = time_spreading(E2ref, rate, fC)

    Mref = threshold(Eref)
    test_modulationIn = ModulationIn(
        e2_tmp=Etmptest, etilde_tmp=Etmptest, eder_tmp=np.zeros_like(Etmptest)
    )
    ref_modulationIn = ModulationIn(
        e2_tmp=Etmpref, etilde_tmp=Etmpref, eder_tmp=np.zeros_like(Etmpref)
    )
    Modtest, test_modulationIn = modulation(
        E2test, rate, in_struct=test_modulationIn, fC=fC
    )
    Modref, _ = modulation(E2ref, rate, in_struct=ref_modulationIn, fC=fC)

    proc = Processing(
        fftref,
        ffttest,
        ffteref,
        fftetest,
        fnoise,
        pptest,
        ppref,
        ppnoise,
        E2test,
        E2ref,
        Etest,
        Eref,
        Mref,
        Modtest,
        Modref,
    )

    # compute MOVs for this block
    movs = MOV()

    if boundflag:
        bandwidth_out = bandwidth(proc, out=state)
        movs.update(
            BandwidthRefb=bandwidth_out["BandwidthRefb"],
            BandwidthTestb=bandwidth_out["BandwidthTestb"],
        )
        # update corresponding state values in bandwidth_result
        map(lambda x: state.update({x: bandwidth_out[x]}), bandwidth_out.keys())
        NMR_out, nmrtmp = nmr(proc, state)
        state.update({"nmrtmp": nmrtmp})
        movs.update(TotalNMRb=movs.TotalNMRb + NMR_out)
        reldist, reldisttmp = reldistframes(proc, state)
        state.update({"RelDistFramesb": reldist})
        movs.update(RelDistFramesb=reldist)
        state["countboundary"] += 1
        if energyth(test=ch1test, ref=ch1ref):
            hs, ehstmp = harmstruct(
                proc,
                state,
            )
            movs.update(EHSb=hs)

    if state["count"] > delaytime2:
        mouts = moddiff(Modtest, Modref, ref_modulationIn.Etildetmp, fC)
        # pdb.set_trace()
        ModDiff = ModDiffOut(mouts[0], mouts[1], mouts[2])
        ModDiffInVars = ModDiffIn()
        o = ModDiff1(ModDiff, ModDiffInVars, state["count"] - delaytime2)
        movs.update(WinModDiff1b=o[0])
        ModDiffInVars = o[1]
        md2, ModDiffInVars = ModDiff2(ModDiff, ModDiffInVars)
        movs.update(AvgModDiff1b=md2)
        md3, ModDiffInVars = ModDiff3(ModDiff, ModDiffInVars)
        movs.update(AvgModDiff2b=md3)
        Ntotaltest = loudness(Etest, fC=fC, bark=BARK)
        Ntotalref = loudness(Eref, fC=fC, bark=BARK)
        noise = 0
        if Ntotaltest > 0.1 or Ntotalref > 0.1:
            noise = 1
        if noise and state["internal_count"] <= delaytime1:
            state["internal_count"] += 1
            state["loudcounter"] += 1
        else:
            levadaptin = LevPatAdaptIn(bark=BARK)
            levadaptin.Ptest = pptest
            levadaptin.Pref = ppref
            lev = levpatadapt(
                Etest,
                Eref,
                rate,
                hann=HANN,
                fC=fC,
                Tmin=0.008,
                T100=0.03,
                tmp=levadaptin,
            )
            nltemp = 0

            n_l, nltemp = noiseloudness(
                Modtest,
                Modref,
                lev,
                nltemp,
                state["count"] - delaytime2 - state["loudcounter"],
                fC=fC,
            )
            state["nltemp"] = nltemp
            movs.update(RmsNoiseLoudb=n_l)

    ADBb, PMtemp, Ptildetemp, Qsum, ndistorcedtmp = detprob(Etest, Eref, state)
    state.update(
        {"PMtemp": PMtemp, "Ptildetemp": Ptildetemp, "ndistorcedtmp": ndistorcedtmp}
    )
    movs.update(ADBb=ADBb)
    movs.update(MFPDb=PMtemp)
    # convert MOVs to a dict
    mov_dict = movs.to_dict()
    neural_out = neural(mov_dict)
    DI = neural_out["DI"]
    ODG = neural_out["ODG"]
    return proc, state, movs, DI, ODG


def init_state():
    return {
        "countboundary": 1,
        "RelDistFramesb": 0,
        "nmrtmp": 0,
        "countenergy": 1,
        "EHStmp": 0,
        "nltmp": 0,
        "noise": 0,
        "internal_count": 0,
        "loudcounter": 0,
        "sumBandwidthRefb": 0,
        "sumBandwidthTestb": 0,
        "countref": 0,
        "counttest": 0,
        "BandwidthRefb": 0,
        "BandwidthTestb": 0,
        "count": 1,
        "RelDistTmp": 0,
        "CFFTtemp": 0,
        "Ptildetemp": 0,
        "PMtmp": 0,
        "QSum": 0,
        "ndistorcedtmp": 0,
        "Cffttmp": np.zeros(1024),
    }


def process_audio_files(ref_filename: str, test_filename: str):
    def read_and_process_soundfile(filename: str) -> np.ndarray:
        sound_file = SoundFile(filename)
        sound_blocks = np.array(read_wav_blocks(filename))
        # mono
        if sound_blocks.shape[-1] == 1:
            sound_blocks = np.squeeze(sound_blocks, axis=-1)
        # stereo
        elif sound_blocks.shape[-1] == 2:
            sound_blocks = sound_blocks.mean(axis=-1)
        return sound_blocks, sound_file.samplerate

    ref_blocks, ref_rate = read_and_process_soundfile(ref_filename)
    test_blocks, test_rate = read_and_process_soundfile(test_filename)

    # 确保两个音频文件的块数相同
    min_blocks = min(len(ref_blocks), len(test_blocks))
    ref_blocks = ref_blocks[:min_blocks]
    test_blocks = test_blocks[:min_blocks]

    processed_blocks_list = []
    state = init_state()
    result = {"MOV_list": [], "DI_list": [], "ODG_list": []}

    for i in tqdm(range(min_blocks)):
        boundaryflag = boundary(ref_blocks[i], test_blocks[i], ref_rate)
        proc, state, movs, di, odg = process_audio_block(
            ref_blocks[i],
            test_blocks[i],
            rate=ref_rate,
            state=state,
            boundflag=boundaryflag,
            test_rate=test_rate,
        )
        result["MOV_list"].append(movs)
        processed_blocks_list.append(proc)
        result["DI_list"].append(di)
        result["ODG_list"].append(odg)
        state["count"] += 1

    avg_DI = np.mean(result["DI_list"])
    avg_ODG = np.mean(result["ODG_list"])
    return avg_ODG  # 只返回 ODG 值


if __name__ == "__main__":
    process_audio_files("ref.wav", "test.wav")
