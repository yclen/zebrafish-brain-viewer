"""Build data/regions.json for the viewer.

Usage:  python -I make_regions_json.py <items.json from list_meshes.py> <out regions.json>

Combines the mapZebrain v2.0 (MECE, 2024) region hierarchy with short plain-language
function summaries written for this viewer. The summaries are a learning aid, not a
substitute for the literature: larval zebrafish circuit roles are still being mapped.
"""
import json
import re
import sys

# --- Plain-language function summaries, keyed by mapZebrain region id ----------------
D = {
    # Forebrain
    1102: "The front of the three primary brain divisions. It contains the telencephalon (smell, learning, emotion-like states, action selection) and the diencephalon (hypothalamus, thalamus, pretectum, habenula, pineal), which together handle body state, hormones and much of the early processing of light and vision.",
    1142: "The front end of the brain. In fish it develops by folding outward (eversion) rather than inward like the mammalian cortex, so its 'cortex-like' part sits on top. It combines smell with other senses and supports learning and motivated behaviour.",
    1139: "First relay station for smell. Axons from odour-sensing neurons in the nose end here in small clusters (glomeruli), each tuned to particular odour molecules; the bulb then sends odour information on to the telencephalon, hypothalamus and habenula.",
    1078: "The dorsal telencephalon, the fish counterpart of the mammalian cortex, hippocampus and amygdala. In adult fish the lateral pallium is linked to spatial learning (hippocampus-like) and the medial pallium to emotional/avoidance learning (amygdala-like). In larvae it is still maturing but already responds to sensory input and is involved in learned and social behaviour.",
    1046: "The ventral telencephalon, comparable to the mammalian basal ganglia and septum (striatum, pallidum). It is thought to help select and motivate actions, and contains inhibitory (GABAergic) and neuromodulatory neurons.",
    1098: "Master controller of internal state: hunger and satiety, stress, sleep and arousal, growth and reproduction. It works through neuropeptides and by commanding the pituitary gland, and in fish it also includes the large inferior lobe involved in feeding.",
    1121: "Front part of the hypothalamus. It contains neuroendocrine and neuromodulatory cell groups, including the hypocretin (orexin) neurons that promote wakefulness in larval zebrafish.",
    1112: "The middle hypothalamus, including the fish-specific inferior lobe. Neurons here respond to prey-like visual stimuli and to food, and activity in this area tracks hunger and feeding behaviour.",
    1077: "The remainder of the intermediate hypothalamus, including the zones lining the third ventricle that contain appetite-regulating neuropeptide neurons (counterparts of the mammalian arcuate nucleus).",
    1079: "Part of the fish inferior lobe. It receives visual input relayed from the pretectum as well as taste-related input, and is implicated in feeding and prey-directed behaviour.",
    1107: "Back part of the hypothalamus, around the posterior recess. It contains dopaminergic and serotonergic neurons that modulate arousal, locomotion and feeding.",
    1047: "The body's master endocrine gland. Under hypothalamic control it releases hormones such as ACTH (stress / cortisol), growth hormone, prolactin, thyroid-stimulating hormone and the gonadotropins.",
    1044: "Contains the neurosecretory preoptic nucleus, which makes oxytocin, vasopressin (isotocin/vasotocin in fish) and CRH. It drives the stress-hormone axis and influences social behaviour and body-fluid balance. Retinal arborization fields AF1 and AF2 sit inside it.",
    1106: "Basal diencephalon. Contains dopaminergic neuron clusters whose long axons descend to the hindbrain and spinal cord, where they modulate how readily the larva swims (comparable to the mammalian A11 group).",
    1051: "The light-sensing part of the eye and, developmentally, an outgrowth of the brain. Photoreceptors feed bipolar cells, which feed retinal ganglion cells; ganglion-cell axons form the optic nerve and end in ten 'arborization fields' (AF1-AF10), the largest being the optic tectum (AF10).",
    1061: "A small region between the thalamus and the telencephalon. Its ventral entopeduncular nucleus sends input to the habenula.",
    1100: "Projects to the habenula, much like the mammalian entopeduncular nucleus / internal globus pallidus that passes reward- and punishment-related signals to the lateral habenula.",
    1103: "The rest of the eminentia thalami outside the ventral entopeduncular nucleus.",
    1135: "Also called the ventral thalamus. Mostly inhibitory (GABAergic) neurons, comparable to the mammalian zona incerta and thalamic reticular nucleus, which shape and gate sensory signals. Contains retinal arborization field AF3.",
    1053: "Relays and filters sensory signals, especially visual ones, and passes them to the telencephalon and other areas. This parent region also includes the pineal complex and the habenula (the epithalamus).",
    1060: "A light-sensitive organ on top of the brain that produces melatonin at night, helping set circadian (day/night) rhythms and sleep.",
    1063: "A hub that links the forebrain to the brainstem's neuromodulatory centres (interpeduncular nucleus, raphe) through the fasciculus retroflexus. It is involved in fear and anxiety, coping with stress, and switching between behavioural strategies. It is left-right asymmetric in zebrafish.",
    1067: "Equivalent of the mammalian medial habenula. Left and right sides differ in size and inputs (e.g. light versus odour). It projects to the interpeduncular nucleus and is linked to fear responses and anxiety-like behaviour.",
    1116: "Equivalent of the mammalian lateral habenula. It signals worse-than-expected outcomes and, through the raphe, can push behaviour from active coping toward passivity.",
    1045: "The thalamic nuclei proper. They relay visual and other sensory information onward; some respond to looming (approaching-object) stimuli and changes in light. Contains retinal arborization field AF4.",
    1127: "A visual area that receives direct retinal input (AF5-AF9). It detects whole-field motion and drives the optomotor response (swimming to hold position in a flow) and the optokinetic eye movements that stabilize the image on the retina; it also contributes to prey detection.",
    1153: "The back part of the posterior tuberculum (basal prosomere 2). It is part of the basal diencephalon that contains dopaminergic neurons. (No 3D mesh is provided for this region in the current atlas version.)",
    1154: "The area around the nucleus of the medial longitudinal fascicle (basal prosomere 1). (No 3D mesh is provided for this region in the current atlas version.)",
    1155: "A cluster of large reticulospinal neurons that project to the spinal cord and control swim speed and posture, and help steer. (No 3D mesh is provided for this region in the current atlas version.)",

    # Midbrain
    1130: "The middle of the three primary brain divisions. It contains the optic tectum (the main visual centre), the tori (hearing/lateral line and vision-related) and the tegmentum (motor control, including eye movements).",
    1049: "The ventral midbrain. It contains motor nuclei for the eyes and premotor neurons that help start and steer swimming.",
    1122: "Medial tegmentum, including the oculomotor nucleus.",
    1129: "The rest of the medial tegmentum: premotor and reticular neurons that contribute to locomotion and eye-movement control.",
    1137: "Cranial nerve III motor neurons. They drive most of the eye muscles, including the inward eye rotation (convergence) larvae use to lock on to prey during hunting.",
    1131: "Lateral midbrain tegmentum, containing premotor and relay neurons involved in movement control.",
    1080: "The roof of the midbrain: the optic tectum plus the torus longitudinalis and torus semicircularis.",
    1088: "A fish-specific ridge along the tectum's midline. It sends input to the tectum's outermost layer and is thought to help with visual processing such as anticipating changes caused by the fish's own movements.",
    1097: "The optic tectum (counterpart of the mammalian superior colliculus) is the largest visual centre. It holds a map of visual space and detects prey (small moving dots) and threats (looming shapes), then triggers orienting or escape.",
    1087: "The layered sheet of fibres and synapses in the tectum (retinal arborization field AF10), where most retinal ganglion-cell axons end. Different layers receive different types of visual information.",
    1054: "The boundary zone between the SFGS and SGC layers of the tectal neuropil.",
    1073: "The outermost fibre layer of the tectum: incoming retinal axons running toward their target layers.",
    1094: "The main retinal input layer of the tectum, subdivided into sublayers that receive different visual features (e.g. direction, size).",
    1118: "The outermost tectal layer, which receives input from the torus longitudinalis rather than the retina.",
    1123: "The boundary zone between the SAC layer and the periventricular cell layer of the tectum.",
    1140: "A deep tectal layer of fibres. It includes outputs leaving the tectum and inputs from other brain areas.",
    1146: "A deep tectal layer with retinal input and the dendrites of tectal neurons that send output toward premotor areas.",
    1095: "The tectum's cell-body layer. Its densely packed periventricular neurons extend dendrites into the neuropil and perform most of the tectum's computations.",
    1113: "The main auditory and lateral-line (water-movement sense) centre of the midbrain, comparable to the mammalian inferior colliculus.",

    # Hindbrain
    1148: "The back of the three primary brain divisions, made of the cerebellum and the medulla oblongata. It holds the cranial-nerve nuclei, the escape circuit (Mauthner cells), the rhythm generators for swimming and eye movements, and the major neuromodulatory nuclei (raphe, locus coeruleus).",
    1085: "Coordinates and fine-tunes movements and learns from errors: for example, adjusting swimming and eye movements, and contributing to classical (fear) conditioning. It receives 'error' signals from the inferior olive.",
    1096: "The brainstem proper. It contains cranial-nerve motor and sensory nuclei, the reticular formation (including the reticulospinal neurons that command swimming and escape, such as the Mauthner cells), and centres that control autonomic functions such as heart rate.",
    1068: "The front (rostral) part of the medulla oblongata, including the isthmus region with the raphe, locus coeruleus, nucleus isthmi and the trigeminal and trochlear motor nuclei.",
    1065: "The dorsal (sensory and stripe) part of the rostral medulla.",
    1093: "Stripe 1 of the rostral dorsal medulla, including the trochlear motor nucleus.",
    1062: "Cranial nerve IV motor neurons, which control the superior oblique eye muscle (eye rotation and depression).",
    1072: "The first relay for lateral-line input: it processes the water-movement signals detected by neuromasts along the head and body.",
    1134: "The ventral part of the rostral medulla: the raphe, locus coeruleus, interpeduncular nucleus, nucleus isthmi, trigeminal motor nuclei and surrounding reticular neurons.",
    1089: "Part of the cranial nerve V motor nucleus, which controls jaw muscles for feeding (suction capture of prey).",
    1144: "Part of the cranial nerve V motor nucleus, which controls jaw muscles for feeding.",
    1092: "The main serotonergic (5-HT) nucleus, comparable to the mammalian dorsal raphe. It sends serotonin widely across the brain and regulates arousal, sleep, feeding state and switching between exploring and exploiting.",
    1120: "The main target of the dorsal habenula. It regulates anxiety-like behaviour, locomotion and connections with the raphe.",
    1138: "A small noradrenergic nucleus that sends noradrenaline throughout the brain. It controls arousal and alertness, and in larvae it helps signal when effort is futile so that the animal gives up and stops swimming.",
    1145: "The remainder of the rostral ventral medulla, mainly reticular neurons.",
    1147: "Connected back and forth with the optic tectum. It is thought to support visual attention, selecting which stimulus to respond to (comparable to the mammalian parabigeminal nucleus).",
    1149: "The glutamatergic (excitatory) part of the nucleus isthmi.",
    1150: "A cholinergic part of the nucleus isthmi, which provides acetylcholine to the tectum.",
    1151: "The GABAergic (inhibitory) part of the nucleus isthmi.",
    1152: "A cholinergic part of the nucleus isthmi, which provides acetylcholine to the tectum.",
    1143: "The middle part of the medulla oblongata, including the abducens and facial motor nuclei.",
    1081: "The ventral part of the middle medulla, including the abducens and facial motor nuclei and reticular neurons.",
    1055: "The remainder of the middle ventral medulla, mainly reticular and premotor neurons.",
    1071: "Cranial nerve VII motor neurons. In fish they control the muscles of the hyoid arch and gill cover (operculum), which are used in feeding and breathing movements.",
    1086: "Cranial nerve VI motor neurons. They drive the lateral rectus muscle, which turns the eye outward and is central to saccades and the optokinetic reflex.",
    1110: "The dorsal part of the middle medulla, organized in stripes.",
    1104: "The back (caudal) part of the medulla oblongata, next to the spinal cord. It contains vagal sensory and motor centres, the area postrema, the inferior olive and the inferior raphe.",
    1050: "The dorsal part of the caudal medulla, containing the vagal sensory lobe, vagus motor nucleus and area postrema.",
    1074: "Cranial nerve X motor neurons. They control the pharynx and gill arches and send autonomic output to the heart and gut.",
    1111: "A chemosensory zone outside the blood-brain barrier that detects toxins and circulating signals. It triggers aversive responses (in mammals, nausea and vomiting).",
    1136: "Receives taste and visceral (internal-organ) sensory input from the mouth, pharynx and gut via the vagus nerve.",
    1059: "The ventral part of the caudal medulla: the inferior olive, inferior raphe, lateral reticular nucleus and reticular neurons.",
    1052: "Sends climbing fibres to the cerebellum, which carry 'error' or teaching signals that drive motor learning.",
    1064: "A serotonergic nucleus whose projections reach the spinal cord, where they modulate swimming and other motor output.",
    1133: "Relays information about ongoing movement from the spinal cord and brainstem to the cerebellum.",
    1141: "The remainder of the caudal ventral medulla, mainly reticular and premotor neurons.",

    # Peripheral nervous system
    1101: "Sensory ganglia and organs outside the brain that are included in the atlas.",
    1048: "The lining of the nose, where odour-receptor neurons detect smells and send their axons to the olfactory bulb.",
    1056: "Sensory neurons that innervate lateral-line neuromasts on the head and sense water movement.",
    1091: "Sensory neurons for touch, temperature and pain on the head (cranial nerve V). They trigger escape responses to touch on the head.",
    1108: "Sensory neurons that innervate lateral-line neuromasts along the body and tail.",
    1125: "Cranial nerve VIII sensory neurons that innervate the inner ear and carry hearing and balance (vestibular) signals.",
    1132: "Cranial nerve IX sensory neurons, which carry taste and sensation from the pharynx.",
}

AF = {
    1117: 1, 1075: 2, 1084: 3, 1083: 4, 1076: 5, 1115: 6, 1128: 7, 1119: 8, 1126: 9,
}
AF_NOTE = {
    7: " AF7 is best known for responding to small, prey-like moving objects, and helps trigger hunting.",
}

def describe(item):
    rid = item["id"]
    if rid in D:
        return D[rid]
    if rid in AF:
        n = AF[rid]
        return (f"Retinal arborization field {n} (AF{n}): one of ten zones where retinal ganglion-cell axons end. "
                "AF1-AF9 lie outside the tectum and mostly serve non-image-forming and reflexive visual functions; "
                "the roles of individual fields are still being worked out." + AF_NOTE.get(n, ""))
    m = re.search(r"(superior|intermediate|inferior) dorsal medulla oblongata stripe ([\d&]+)", item["name"])
    if m:
        return (f"One of the longitudinal stripes of the {m.group(1)} dorsal medulla. Hindbrain neurons are arranged in "
                "stripes by transmitter type and developmental age. The dorsal stripes include sensory and "
                "premotor neurons; this stripe is an anatomical landmark rather than a single functional unit.")
    if "(remaining)" in item["name"]:
        return "The rest of the parent region, after its named subregions are taken out."
    return ""

DIVISION = {1102: "forebrain", 1130: "midbrain", 1148: "hindbrain", 1101: "peripheral"}

def main(items_path, out_path):
    items = json.load(open(items_path, encoding="utf-8"))
    by_id = {i["id"]: i for i in items}
    def division(i):
        while i["parent"] is not None:
            i = by_id[i["parent"]]
        return DIVISION.get(i["id"], "other")
    regions = []
    missing = []
    for i in items:
        if i["id"] == 0:
            continue
        desc = describe(i)
        if not desc:
            missing.append(i["name"])
        regions.append(dict(
            id=i["id"], name=i["name"], parent=i["parent"], depth=i["depth"],
            children=i["children"], color=i["color"], hasMesh=bool(i.get("bytes")),
            division=division(i), description=desc,
        ))
    out = dict(
        source="mapZebrain (mapzebrain.org), region annotation version 2.0 (MECE, 2024); Kunst et al. 2019, Neuron 103:21-38",
        units="micrometres, larval zebrafish at 6 days post-fertilization",
        axes={"x": "left-right", "y": "anterior (low) to posterior (high)", "z": "ventral (low) to dorsal (high)"},
        outlineMesh="0",
        regions=regions,
    )
    json.dump(out, open(out_path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(len(regions), "regions written;", "missing descriptions:", missing or "none")

if __name__ == "__main__":
    main(*sys.argv[1:3])
