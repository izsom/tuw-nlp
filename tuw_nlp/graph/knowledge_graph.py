import networkx as nx
from collections import Counter
import re
import nltk
import stanza
from nltk.corpus import wordnet as wn

from nltk.wsd import lesk
from pywsd.lesk import simple_lesk, cosine_lesk, adapted_lesk, original_lesk

from tuw_nlp.text.pipeline import CachedStanzaPipeline
import os
import conceptnet_lite
from conceptnet_lite import Label, Concept, edges_between
from conceptnet_lite.db import RelationName
from networkx.algorithms.isomorphism import DiGraphMatcher


basepath = os.path.dirname(__file__)
nltk.download('wordnet')
if not os.path.exists(os.path.join(basepath, "conceptnet/conceptnet.db")):
    answer = input("Would you like to download ConceptNet? It might take more than an hour. (Yes/no)")
    if not answer.lower().startswith('n'):
        use_conceptnet = True
        conceptnet_lite.connect(os.path.join(basepath, "conceptnet/conceptnet.db"), db_download_url=None)
    else:
        use_conceptnet = False
else:
    use_conceptnet = True
    conceptnet_lite.connect(os.path.join(basepath, "conceptnet/conceptnet.db"))


class KnowledgeNode(str):

    # We need this because penman will not recognise it as atomic otherwise
    def __new__(cls, *args, **kwargs):
        obj = super(KnowledgeNode, cls).__new__(cls, args[0])
        return obj

    def __init__(self, text, lemma, synset, concept, pos):
        self.text = text
        self.lemma = lemma
        self.synset = synset
        self.concept = concept
        self.pos = pos
        self.antonym = self.get_antonym()

    def get_antonym(self):
        if 'antonym' in self.__dict__:
            return self.antonym
        if self.synset is None:
            return None
        lemma = self.synset.lemmas()
        if len(lemma) == 0:
            return None
        antonym = lemma[0].antonyms()
        if len(antonym) == 0:
            return None
        return antonym[0].synset()

    def concept_connection(self, other):
        if self.concept is not None and other.concept is not None and self.synset is not None:
            return [e for e in edges_between(self.concept, other.concept)]
        else:
            return []

    def reg_match(self, text):
        if text.lower() == self.text.lower():
            return True
        text_match = re.findall(text.lower(), self.text.lower()) + re.findall(text.lower(), self.lemma.lower())
        if len(text_match) == 0:
            return False
        return True

    def similarity(self, other):
        if self.synset is None or other.synset is None:
            return 1 if self.text == other.text or self.lemma == other.lemma else 0
        # As per issue: https://github.com/alvations/pywsd/issues/54
        similarity = wn.synset(self.synset.name()).wup_similarity(wn.synset(other.synset.name()))
        if self.antonym is not None:
            antonym_similarity = self.antonym.wup_similarity(other.synset)
            if antonym_similarity > similarity:
                return -antonym_similarity
        return similarity

    def __str__(self):
        if self.synset is None:
            return self.text
        return self.synset._name

    def __eq__(self, other):
        if isinstance(other, KnowledgeNode):
            text_match = self.reg_match(other.text)
            if text_match:
                return True
            syn_similarity_rate = 0
            concept_weight = 0 if (self.concept != other.concept or self.concept is None) else 1  # There are no concept edges between the same concepts
            if self.synset is not None and other.synset is not None:
                syn_similarity_rate = wn.synset(self.synset.name()).wup_similarity(wn.synset(other.synset.name()))
            if self.concept is not None and other.concept is not None:
                concept_connections = self.concept_connection(other)
                antonyms = [e for e in concept_connections if e.relation.name == "antonym"] + \
                           [e for e in concept_connections if e.relation.name == "distinct_from"]
                related = [e for e in concept_connections if e.relation.name == "is_a"] + \
                          [e for e in concept_connections if e.relation.name == "related_to"]
                if len(antonyms) > 0:
                    concept_weight = -1
                elif len(concept_connections) != 0:
                    concept_weight = len(related) / len(concept_connections)
            return (syn_similarity_rate + concept_weight) / 2 >= 0.5
        elif isinstance(other, str):
            if re.match(r'[a-zA-Z_]+\.[arsnv]\.[0-9]{2}', other) is not None and self.synset is not None:
                try:
                    synset = wn.synset(other)
                    if synset.wup_similarity(wn.synset(self.synset.name())) >= 0.8:
                        return True
                except ValueError:
                    print(other)
                return False
            return self.reg_match(other)

    def __hash__(self):
        if self.concept is not None:
            return hash(self.lemma) + hash(tuple(self.concept)) + hash(self.synset)
        return hash(self.lemma) + hash(self.concept) + hash(self.synset)

    def __setstate__(self, state):
        self.text = state["text"]
        self.lemma = state["lemma"]
        self.synset = None if state["synset"] is None else wn.synset(state["synset"])
        self.antonym = None if state["antonym"] is None else wn.synset(state["antonym"])
        self.concept = None if state["concept"] is None else [Concept.get_by_id(concept) for concept in state["concept"]]
        self.pos = None if "pos" not in state else state["pos"]
    
    def __getstate__(self):
        return {
            "text": self.text,
            "lemma": self.lemma,
            "synset": None if self.synset is None else self.synset.name(),
            "antonym": None if self.antonym is None else self.antonym.name(),
            "concept": None if self.concept is None else [c.id for c in self.concept],
            "pos": self.pos
        }


class KnowledgeGraph:
    def __init__(self, graph=None, text=None, pipeline=None, lang="en", synset_method="vote_lesk"):
        self.synset_methods = {
            "vote_lesk": self.vote_lesk,
            "first_synset": self.first_synset,
            "nltk_lesk": self.nltk_lesk,
            "original_lesk": self.original_lesk,
            "simple_lesk": self.simple_lesk,
            "cosine_lesk": self.cosine_lesk,
            "adapted_lesk": self.adapted_lesk,
            "graph_match": self.graph_match,
            "ud_match": self.ud_match
        }
        self.wn_lexnames = ["adj.all", # 	all adjective clusters
                            "adj.pert", # 	relational adjectives (pertainyms)
                            "adv.all", #	all adverbs
                            "noun.Tops", # 	unique beginner for nouns
                            "noun.act", # 	nouns denoting acts or actions
                            "noun.animal", # 	nouns denoting animals
                            "noun.artifact", # 	nouns denoting man-made objects
                            "noun.attribute", # 	nouns denoting attributes of people and objects
                            "noun.body", # 	nouns denoting body parts
                            "noun.cognition", # 	nouns denoting cognitive processes and contents
                            "noun.communication", # 	nouns denoting communicative processes and contents
                            "noun.event", # 	nouns denoting natural events
                            "noun.feeling", # 	nouns denoting feelings and emotions
                            "noun.food", # 	nouns denoting foods and drinks
                            "noun.group", # 	nouns denoting groupings of people or objects
                            "noun.location", # 	nouns denoting spatial position
                            "noun.motive", # 	nouns denoting goals
                            "noun.object", # 	nouns denoting natural objects (not man-made)
                            "noun.person", # 	nouns denoting people
                            "noun.phenomenon", # 	nouns denoting natural phenomena
                            "noun.plant", # 	nouns denoting plants
                            "noun.possession", # 	nouns denoting possession and transfer of possession
                            "noun.process", # 	nouns denoting natural processes
                            "noun.quantity", # 	nouns denoting quantities and units of measure
                            "noun.relation", # 	nouns denoting relations between people or things or ideas
                            "noun.shape", # 	nouns denoting two and three dimensional shapes
                            "noun.state", # 	nouns denoting stable states of affairs
                            "noun.substance", # 	nouns denoting substances
                            "noun.time", # 	nouns denoting time and temporal relations
                            "verb.body", # 	verbs of grooming, dressing and bodily care
                            "verb.change", # 	verbs of size, temperature change, intensifying, etc.
                            "verb.cognition", # 	verbs of thinking, judging, analyzing, doubting
                            "verb.communication", # 	verbs of telling, asking, ordering, singing
                            "verb.competition", # 	verbs of fighting, athletic activities
                            "verb.consumption", # 	verbs of eating and drinking
                            "verb.contact", # 	verbs of touching, hitting, tying, digging
                            "verb.creation", # 	verbs of sewing, baking, painting, performing
                            "verb.emotion", # 	verbs of feeling
                            "verb.motion", # 	verbs of walking, flying, swimming
                            "verb.perception", # 	verbs of seeing, hearing, feeling
                            "verb.possession", # 	verbs of buying, selling, owning
                            "verb.social", # 	verbs of political and social activities and events
                            "verb.stative", # 	verbs of being, having, spatial relations
                            "verb.weather", # 	verbs of raining, snowing, thawing, thundering
                            "adj.ppl"] # 	participial adjectives
        self.pos = {'ADJ': wn.ADJ, 'ADV': wn.ADV, 'PART': wn.ADV, 'NOUN': wn.NOUN,
                    'PROPN': wn.NOUN, 'VERB': wn.VERB}
        self.lesk_pos = {'ADJ': 'a', 'ADV': 'r', 'PART': 's', 'NOUN': 'n',
                         'PROPN': 'n', 'VERB': 'v'}
        self.concept_pos = {'ADJ': 'a', 'ADV': 'r', 'NOUN': 'n', 'VERB': 'v'}
        self.parser = pipeline if pipeline is not None \
            else CachedStanzaPipeline(stanza.Pipeline(lang, processors='tokenize,mwt,pos,lemma,depparse'), "cache")
        self.lang = lang
        self.text = text
        self.G = graph
        if self.text is not None and self.G is None:
            self.G = nx.DiGraph()
            self.parse_graph(self.synset_methods[synset_method])
            self.connect_sentence_graphs()

    def get_ud(self, sentences):
        G = nx.DiGraph()
        with self.parser:
            ud_parse = self.parser.parse(sentences)
            for sent_id, sent in enumerate(ud_parse.sentences):
                for word in sent.words:
                    G.add_node(100 * (sent_id + 1) + word.id, name=KnowledgeNode(word.text, word.lemma, None, None, word.upos))
                for dep in sent.dependencies:
                    if dep[0].id != 0:
                        G.add_edge(100 * (sent_id + 1) + dep[0].id, 100 * (sent_id + 1) + dep[2].id, color=dep[1])
        return G, ud_parse
    
    def parse_graph(self, synset_method):
        self.G, self.ud_parse = self.get_ud(self.text)
        for sent_id, sent in enumerate(self.ud_parse.sentences):
                for word in sent.words:
                    self.G.nodes[100*(sent_id+1)+word.id]["name"].concept = self.get_concept(word)
                    self.G.nodes[100*(sent_id+1)+word.id]["name"].synset = self.get_synset(word, sent_id, synset_method)

    def vote_lesk(self, word, synsets, sent_id):
        lesks = [
            self.first_synset(word, synsets, sent_id), 
            self.nltk_lesk(word, synsets, sent_id),
            self.original_lesk(word, synsets, sent_id),
            self.simple_lesk(word, synsets, sent_id),
            self.cosine_lesk(word, synsets, sent_id),
            self.adapted_lesk(word, synsets, sent_id)
            ]
        if len(set(lesks)) != 1:
            return max(Counter(lesks).items(), key=lambda x: x[1])[0]
        return lesks[0]

    def first_synset(self, word, synsets, sent_id):
        return synsets[0]

    def nltk_lesk(self, word, synsets, sent_id):
        return lesk(self.ud_parse.sentences[sent_id].text.split(), word.text, synsets=synsets)

    def original_lesk(self, word, synsets, sent_id):
        return original_lesk(self.ud_parse.sentences[sent_id].text, word.text)
    
    def simple_lesk(self, word, synsets, sent_id):
        return simple_lesk(self.ud_parse.sentences[sent_id].text, word.text, pos=self.lesk_pos[word.pos])
    
    def cosine_lesk(self, word, synsets, sent_id):
        return cosine_lesk(self.ud_parse.sentences[sent_id].text, word.text, pos=self.lesk_pos[word.pos])
    
    def adapted_lesk(self, word, synsets, sent_id):
        return adapted_lesk(self.ud_parse.sentences[sent_id].text, word.text, pos=self.lesk_pos[word.pos])


    @staticmethod
    def node_matcher(n1, n2):
        if n1['name'] is None or n2['name'] is None or \
                n1['name'].pos == 'DET' or n2['name'].pos == 'DET' or \
                n1['name'].pos == 'PUNCT' or n2['name'].pos == 'PUNCT':
            return True
        return n1['name'].pos == n2['name'].pos

    @staticmethod
    def edge_matcher(e1, e2):
        if e1['color'] == 'punct' or e2['color'] == 'punct' or \
           e1['color'] == 'det' or e2['color'] == 'det':
            return True
        if (e1['color'] == 'iobj' and e2['color'] == 'obj') or \
           (e1['color'] == 'obj' and e2['color'] == 'iobj'):
            return True
        return e1['color'].split(':')[0] == e2['color'].split(':')[0]

    def graph_match(self, word, synsets, sent_id):
        goods = {}
        sent_graph = nx.subgraph(self.G, [n for n in self.G.nodes if int(n/100) == sent_id+1])
        for ss in synsets:
            for example in ss.examples():
                ex_graph, _ = self.get_ud(example)
                if DiGraphMatcher(sent_graph, ex_graph, node_match=self.node_matcher,
                                  edge_match=self.edge_matcher).subgraph_is_monomorphic():
                    if ss not in goods:
                        goods[ss] = len(ex_graph.nodes)
                    else:
                        goods[ss] += len(ex_graph.nodes)
        if len(goods) > 0:
            return max(goods.items(), key=lambda x: x[1])[0]
        return synsets[0]
    
    def ud_match(self, word, synsets, sent_id):
        goods = {}
        ud_sentence = self.ud_parse.sentences[sent_id]
        deps = [(dep[0].pos, dep[1], dep[2].pos)for dep in ud_sentence.dependencies if 
                 dep[0].lemma != word.lemma and dep[2].lemma != word.lemma] + \
               [(dep[0].lemma, dep[1], dep[2].pos)for dep in ud_sentence.dependencies if 
                 dep[0].lemma == word.lemma] + \
               [(dep[0].pos, dep[1], dep[2].lemma)for dep in ud_sentence.dependencies if 
                 dep[2].lemma == word.lemma]
        for ss in synsets:
            for example in ss.examples():
                if word.lemma in example or word.text in example:
                    _, ud_parse = self.get_ud(example)
                    sentence = ud_parse.sentences[0]
                    example_deps = [
                        (dep[0].pos, dep[1], dep[2].pos)for dep in sentence.dependencies if 
                         dep[0].lemma != word.lemma and dep[2].lemma != word.lemma] + \
                       [(dep[0].lemma, dep[1], dep[2].pos)for dep in sentence.dependencies if
                         dep[0].lemma == word.lemma] + \
                       [(dep[0].pos, dep[1], dep[2].lemma)for dep in sentence.dependencies if 
                         dep[2].lemma == word.lemma
                         ]
                    overlap = set(deps).intersection(example_deps)
                    if ss not in goods:
                        goods[ss] = len(overlap)
                    else:
                        goods[ss] += len(overlap)
        if len(goods) > 0:
            return max(goods.items(), key=lambda x: x[1])[0]
        return synsets[0]
    
    def get_synset(self, word, sent_id, method):
        synset = None
        if word.pos in self.pos:
            synsets = wn.synsets(word.lemma, pos=self.pos[word.pos])
            if synsets is not None:
                if len(synsets) == 1:
                    synset = synsets[0]
                elif len(synsets) > 1:
                    synset = method(word, synsets, sent_id)
        return synset

    def get_concept(self, word):
        concept = Label.get_or_none(text=word.lemma, language=self.lang)
        if concept is not None:
            concept = concept.concepts
            if word.pos in self.concept_pos:
                concept = [c for c in concept if
                            len(re.findall(f"{self.concept_pos[word.pos]}[/$]*", c.sense_label)) > 0]
        return concept

    def connect_sentence_graphs(self):
        for node, node_data in self.G.nodes(data=True):
            for other_node, other_node_data in self.G.nodes(data=True):
                if int(node/100) != int(other_node/100):
                    concept_connections = node_data["name"].concept_connection(other_node_data["name"])
                    if len(concept_connections) > 0:
                        concept_connection = max(concept_connections, key=lambda x: x.etc["weight"])
                        self.G.add_edge(node, other_node, color=concept_connection.relation.name)

    def similarity(self, other, with_edges=False):
        if not with_edges:
            overlap = len(set([data["data"] for (_, data) in self.G.nodes(data=True)]) & \
                          set([data["data"] for (_, data) in other.G.nodes(data=True)]))
            return overlap / len(set([data["data"] for (_, data) in self.G.nodes(data=True)]))
        else:
            edge_overlap = set([(self.G.nodes(data=True)[f]["data"], self.G.nodes(data=True)[t]["data"],
                                 data["data"]["rel"]) for (f, t, data) in self.G.edges(data=True)]) & \
                           set([(other.G.nodes(data=True)[f]["data"], other.G.nodes(data=True)[t]["data"],
                                 data["data"]["rel"]) for (f, t, data) in other.G.edges(data=True)])
            return len(edge_overlap) / len(self.G.edges)

    @staticmethod
    def d_clean(string):
        s = string
        for c in '\\=@-,\'".!:;<>/{}[]()#^?':
            s = s.replace(c, '_')
        s = s.replace('$', '_dollars').replace('%', '_percent').replace('|', ' ').replace('*', ' ')
        if s == '#':
            s = '_number'
        keywords = ("graph", "node", "strict", "edge")
        if re.match('^[0-9]', s) or s in keywords:
            s = "X" + s
        return s

    def to_dot(self):
        conceptnet_relation_names = [v for v in RelationName.__dict__.values()]
        show_graph = self.G.copy()
        lines = [u'digraph finite_state_machine {', '\tdpi=70;']
        node_id = {n: i for (i, n) in enumerate(show_graph.nodes)}
        node_lines = []
        for node, n_data in show_graph.nodes(data=True):
            printname = self.d_clean(str(n_data['name']))
            node_line = u'\t{0} [shape = circle, label = "{1}"];'.format(node_id[node], printname).replace('-', '_')
            node_lines.append(node_line)
        lines += sorted(node_lines)
        edge_lines = []
        for u, v, edata in show_graph.edges(data=True):
            if edata['color'] not in conceptnet_relation_names:
                edge_lines.append(u'\t{0} -> {1} [ label = "{2}", color = "green"];'.format(node_id[u], node_id[v],
                                                                                            edata['color']))
            else:
                edge_lines.append(u'\t{0} -> {1} [ label = "{2}", color = "blue"];'.format(node_id[u], node_id[v],
                                                                                           edata['color']))
        lines += sorted(edge_lines)
        lines.append('}')
        return u'\n'.join(lines)
