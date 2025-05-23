

import re
from nltk.tokenize import word_tokenize
from nltk.corpus import stopwords
import nltk
from sentence_transformers import SentenceTransformer
from transformers import pipeline
import streamlit as st
nltk.download('punkt_tab')
nltk.download('stopwords')
from neo4j import GraphDatabase
driver = GraphDatabase.driver(URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
from difflib import SequenceMatcher
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
from sklearn.preprocessing import MinMaxScaler
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from nltk.stem import PorterStemmer
from nltk.tokenize import word_tokenize

nltk.download('punkt')


URI="Use your URI"
NEO4J_USERNAME="USERNAME"
NEO4J_PASSWORD="PASSWORD"

stemmer = PorterStemmer()

rebel_tokenizer = AutoTokenizer.from_pretrained("Babelscape/rebel-large")
rebel_model = AutoModelForSeq2SeqLM.from_pretrained("Babelscape/rebel-large")

model = SentenceTransformer("all-MiniLM-L6-v2")

rebel_pipeline = pipeline("text2text-generation", model=rebel_model, tokenizer=rebel_tokenizer, device=0)

def preprocess_text(text):
    text = text.lower()
    text = re.sub(r'[^\w\s]', '', text)  # Remove punctuation
    tokens = word_tokenize(text)
    stop_words = set(stopwords.words("english"))
    filtered_tokens = [word for word in tokens if word not in stop_words]
    return " ".join(filtered_tokens)

def extract_rebel_triples(text):
    output = rebel_pipeline(text, max_length=512, clean_up_tokenization_spaces=True)[0]['generated_text']
    triples = []
    for triple_str in output.split("<triplet>")[1:]:
        parts = triple_str.split("<subj>")
        if len(parts) > 1:
            parts = parts[1].split("<rel>")
            if len(parts) > 1:
                subject = parts[0].strip()
                obj = parts[1].split("<obj>")
                if len(obj) > 1:
                    relation = obj[0].strip()
                    object_ = obj[1].strip()
                    triples.append((subject, relation, object_))
    return triples

def fetch_metadata_for_sections(session, section_ids):
    query = """
    MATCH (s:Section)
    WHERE s.id IN $section_ids
    RETURN s.id AS section_id, s.video_id AS video_id,
           s.start_time AS start_time, s.end_time AS end_time, s.text AS text
    """
    result = session.run(query, section_ids=section_ids)
    return [record.data() for record in result]

def get_kg_matched_section_ids(query_text, driver):
    section_scores = {}

    # Preprocess query
    clean_query = preprocess_text(query_text)

    # Use original for REBEL (since it needs full sentence)
    triples = extract_rebel_triples(query_text)

    with driver.session() as session:
        if triples:
            print(f"[REBEL Triples]: {triples}")
            for subj, rel, obj in triples:
                query = """
                    MATCH (s:Section)-[:MENTIONS]->(e1:Entity),
                          (s)-[:MENTIONS]->(e2:Entity),
                          (e1)-[:RELATION {name: $rel}]->(e2)
                    WHERE e1.name = $subj AND e2.name = $obj
                    RETURN DISTINCT s.section_id AS section_id
                """
                result = session.run(query, {"subj": subj, "rel": rel, "obj": obj})
                for record in result:
                    sec_id = record.get("section_id")
                    if sec_id:
                        section_scores[sec_id] = max(section_scores.get(sec_id, 0), 1.0)

        # Fallback: use cleaned query
        if not section_scores:
            print("[Fallback to Entity Keyword Match]")
            for word in clean_query.split():
                fallback_query = """
                    MATCH (s:Section)-[:MENTIONS]->(e:Entity)
                    WHERE toLower(e.name) CONTAINS $word
                    RETURN DISTINCT s.section_id AS section_id
                """
                result = session.run(fallback_query, {"word": word})
                for record in result:
                    sec_id = record.get("section_id")
                    if sec_id:
                        section_scores[sec_id] = max(section_scores.get(sec_id, 0), 0.6)

    return section_scores

def get_top_k_similar_sections(query_embedding, all_embeddings, section_ids, k=5):
    query_embedding = np.array(query_embedding).reshape(1, -1)
    all_embeddings = np.array(all_embeddings)

    similarities = cosine_similarity(query_embedding, all_embeddings)[0]
    top_indices = similarities.argsort()[::-1][:k]

    top_sections = [section_ids[i] for i in top_indices]
    top_scores = [similarities[i] for i in top_indices]

    return list(zip(top_sections, top_scores))

def fetch_all_section_embeddings(session):
    query = """
    MATCH (s:Section)
    RETURN s.id AS section_id, s.embedding AS embedding
    """
    result = session.run(query)
    section_ids, embeddings = [], []

    for record in result:
        if record["embedding"]:
            section_ids.append(record["section_id"])
            embeddings.append(record["embedding"])

    return section_ids, embeddings

def hybrid_combined_reranker(query_text, model, driver, fetch_all_section_embeddings, get_top_k_similar_sections, fetch_metadata_for_sections, k, alpha):
    # Step 1: Pull section embeddings
    section_ids, embeddings = fetch_all_section_embeddings(driver.session())

    # Step 2: Encode the query
    query_embedding = model.encode(query_text).tolist()

    # Step 3: Top-K vector similarity
    top_matches = get_top_k_similar_sections(query_embedding, embeddings, section_ids, k=len(section_ids))
    vector_scores_dict = dict(top_matches)

    # Normalize vector similarity scores
    vector_scores = np.array(list(vector_scores_dict.values())).reshape(-1, 1)
    scaler = MinMaxScaler()
    normalized_vector_scores = scaler.fit_transform(vector_scores).flatten()
    normalized_vector_scores_dict = dict(zip(vector_scores_dict.keys(), normalized_vector_scores))

    # Step 4: Get KG-based scores
    kg_scores_dict = get_kg_matched_section_ids(query_text, driver)

    # Step 5: Combine scores
    final_scores = []
    for sec_id in section_ids:
        v_score = normalized_vector_scores_dict.get(sec_id, 0)
        k_score = kg_scores_dict.get(sec_id, 0)
        combined_score = alpha * v_score + (1 - alpha) * k_score
        final_scores.append((sec_id, combined_score))

    # Sort by combined score (descending) and pick top-k
    final_scores_sorted = sorted(final_scores, key=lambda x: x[1], reverse=True)[:k]
    top_section_ids_sorted = [sec_id for sec_id, _ in final_scores_sorted]
    combined_score_dict = dict(final_scores_sorted)

    # Step 6: Fetch metadata and match with sorted scores
    metadata = fetch_metadata_for_sections(driver.session(), top_section_ids_sorted)

    # Step 7: Sort metadata based on final scores
    metadata_sorted = sorted(metadata, key=lambda x: combined_score_dict.get(x['section_id'], 0), reverse=True)

    # Step 8: Print results in sorted order
    print("\n===== Final Hybrid Reranked Results (Sorted) =====")
    for meta in metadata_sorted:
        sec_id = meta["section_id"]
        final_score = combined_score_dict.get(sec_id, 0)
        print(f"Section ID : {sec_id}")
        print(f"Video ID   : {meta['video_id']}")
        print(f"Time       : {meta['start_time']} → {meta['end_time']}")
        print(f"Text       : {meta['text']}")
        print(f"Score      : {round(final_score, 4)}")
        print("-" * 50)
        video_path = f"C:/Users/Rimsha Fatima/Downloads/{meta['video_id']}.mp4"  # Replace with the actual path to your videos
        #video_path = r"C:\Users\Rimsha Fatima\Downloads\Computer Networks: Crash Course Computer Science #28.mp4"


        st.video(video_path, start_time=meta['start_time'], end_time=meta['end_time'])


    return final_scores_sorted, metadata_sorted

#K=5
#ground_truth_texts=["many applications usually we use zardad in reverse B full Voltage regulation so in next video I'll explain you zener di as a voltage regulator where I'll explain you parameters of voltage regulator circuits as well as I'll solve few interesting examples if any questions are there please post it over here in comment section I'll be happy to help you thank you so much for watching this video Advantages of Zener Diode\n\n= Less Expensive\n\n= Smaller in Size\n\n= Easily usable in smaller circuits\n\n* Gives protection against over-voltage in reverse bias\n\nDisadvantages of Zener Diode\n= Limited Power Ratings\n= Lower Efficiency in forward bias. {Consumes 0.6 to 0.7V even j\n\nApplications of Zener Diode\n= Voltage Regulator\n\n= Surge Suppressor\n\n* Switching Applications\n\n= Clipper Circuits\n\n \n\x0c",
#                    "zener diode is having so many applications like one can use zener diode as a voltage regulator in Reverse bass while avalan diod that we use it in so many applications like rectifier CLE ER clamper right and usually we operate this Avalanche diod in forward bias applications right so that is how basic comparison is there in between zener and Avalanche diode Application + Voltage Regulator (Reverse Bias ‘+ Rectifier {Forward Bias Application}\n\nApplication}\n\x0c Advantages of Zener Diode\n\n* Less Expensive\n\n* Smaller in Size\n\n= Easily usable in smaller circuits\n\n* Gives protection against over-voltage in reverse bias\n\n \n\x0c",
#                    "first see zener diod is less expensive and usually we use zener diod in small electronic circuits see normal PN Junction diod that we use it in higher power elect El ronics like we can use PN Junction diode in rectifier circuit right but when it comes to zener diod we use it for low power electronic circuits it is less expensive it is smaller in size we can use it in smaller circuit Advantages of Zener Diode\n\n* Less Expensive\n\n* Smaller in Size\n\n= Easily usable in smaller circuits\n\n* Gives protection against over-voltage in reverse bias\n\n \n\x0c",
#                    "with low power configuration and it gives protection against over voltage in Reverse bias but remember this see we use this for electronic circuit where we operate circuits with lower power right see when it comes to disadvantages then one should know zener diode is having limited power ratings we don't use zener diode for high power ratings right and it is having lower efficiency in forward bias see when you keep zener di in forward bias there is always voltage Advantages of Zener Diode\n\n* Less Expensive\n\n= Smaller in Size\n\n= Easily usable in smaller circuits\n\n* Gives protection against over-voltage in reverse bias\n\n \n\x0c",
#                    "drop of 0.6 to 0.7 in case of silicon zardad right so it is having low lower efficiency in terms of power consumption that one can say right when it comes to Applications there are so many applications with Zen as I have told you we can use that as voltage regulator in Reverse Bas we can use it for surge compressor normal switching applications are also there as it is there with p and Junction diode we can use that in Clipper circuits as well to clip the waveform right so there are so Advantages of Zener Diode\n\n* Less Expensive\n\n= Smaller in Size\n\n= Easily usable in smaller circuits\n\n* Gives protection against over-voltage in reverse bias\n\nDisadvantages of Zener Diode\n« Limited Power Ratings\n= Lower Efficiency in forward bias. {Consumes 0.6 to 0.7V even i\n\n \n\x0c"]
#K=3
ground_truth_texts=["many applications usually we use zardad in reverse B full Voltage regulation so in next video I'll explain you zener di as a voltage regulator where I'll explain you parameters of voltage regulator circuits as well as I'll solve few interesting examples if any questions are there please post it over here in comment section I'll be happy to help you thank you so much for watching this video Advantages of Zener Diode\n\n= Less Expensive\n\n= Smaller in Size\n\n= Easily usable in smaller circuits\n\n* Gives protection against over-voltage in reverse bias\n\nDisadvantages of Zener Diode\n= Limited Power Ratings\n= Lower Efficiency in forward bias. {Consumes 0.6 to 0.7V even j\n\nApplications of Zener Diode\n= Voltage Regulator\n\n= Surge Suppressor\n\n* Switching Applications\n\n= Clipper Circuits\n\n \n\x0c",
                    "zener diode is having so many applications like one can use zener diode as a voltage regulator in Reverse bass while avalan diod that we use it in so many applications like rectifier CLE ER clamper right and usually we operate this Avalanche diod in forward bias applications right so that is how basic comparison is there in between zener and Avalanche diode Application + Voltage Regulator (Reverse Bias ‘+ Rectifier {Forward Bias Application}\n\nApplication}\n\x0c Advantages of Zener Diode\n\n* Less Expensive\n\n* Smaller in Size\n\n= Easily usable in smaller circuits\n\n* Gives protection against over-voltage in reverse bias\n\n \n\x0c",
                    "drop of 0.6 to 0.7 in case of silicon zardad right so it is having low lower efficiency in terms of power consumption that one can say right when it comes to Applications there are so many applications with Zen as I have told you we can use that as voltage regulator in Reverse Bas we can use it for surge compressor normal switching applications are also there as it is there with p and Junction diode we can use that in Clipper circuits as well to clip the waveform right so there are so Advantages of Zener Diode\n\n* Less Expensive\n\n= Smaller in Size\n\n= Easily usable in smaller circuits\n\n* Gives protection against over-voltage in reverse bias\n\nDisadvantages of Zener Diode\n« Limited Power Ratings\n= Lower Efficiency in forward bias. {Consumes 0.6 to 0.7V even i\n\n \n\x0c"]
#K=1
#ground_truth_texts=["There are so many applications with Zen as I have told you we can use that as voltage regulator in Reverse Bas we can use it for surge compressor normal switching applications are also there as it is there with p and Junction diode we can use that in Clipper circuits as well to clip the waveform right so there are so many applications usually we use zardad in reverse B full Voltage regulation so in next video I'll explain you zener di as a voltage regulator where I'll explain you parameters of voltage regulator circuits as well as I'll solve few interesting examples if any questions are there please post it over here in comment section I'll be happy to help you thank you so much for watching this video Advantages of Zener Diode\n\n= Less Expensive\n\n= Smaller in Size\n\n= Easily usable in smaller circuits\n\n* Gives protection against over-voltage in reverse bias\n\nDisadvantages of Zener Diode\n= Limited Power Ratings\n= Lower Efficiency in forward bias. {Consumes 0.6 to 0.7V even j\n\nApplications of Zener Diode\n= Voltage Regulator\n\n= Surge Suppressor\n\n* Switching Applications\n\n= Clipper Circuits\n\n \n\x0c"]
def clean_text(text):
    # Remove special chars, extra spaces, and truncate
    text = re.sub(r'[^\w\s]', '', text)  # Remove symbols
    text = re.sub(r'\s+', ' ', text)     # Collapse multiple spaces
    return text.strip().lower()[:100]    # Take first 100 chars (adjust as needed)

# Example cleaning
ground_truth_texts_cleaned = [clean_text(text) for text in ground_truth_texts]

# Remove duplicates (if any)
ground_truth_texts_cleaned = list(set(ground_truth_texts_cleaned))

def calculate_mrr_hybrid(final_scores_sorted, ground_truth_texts, metadata_sorted,
                        similarity_threshold=0.3, debug=False):

    # --- Text Cleaning (Same as Recall@K) ---
    def clean_text(text):
        text = re.sub(r'[^\w\s]', '', str(text).lower())
        text = re.sub(r'\s+', ' ', text).strip()
        return text # Focus on first 100 chars for comparison

    # Clean ground truths and remove duplicates
    ground_truth_cleaned = list(set([clean_text(text) for text in ground_truth_texts]))

    # Clean retrieved texts
    ranked_results = [
        (rank, clean_text(meta['text']), score)
        for rank, ((_, score), meta) in enumerate(zip(final_scores_sorted, metadata_sorted), start=1)
    ]

    reciprocal_ranks = []

    for truth_text in ground_truth_cleaned:
        best_rank = None
        best_similarity = 0

        # Find best match across all results
        for rank, text, score in ranked_results:
            similarity = SequenceMatcher(None, truth_text, text).ratio()
            if similarity > best_similarity:
                best_similarity = similarity
                best_rank = rank

        if best_rank is not None and best_similarity >= similarity_threshold:
            # Scale reciprocal rank by similarity (soft threshold)
            weighted_reciprocal = best_similarity * (1.0 / best_rank)
            reciprocal_ranks.append(weighted_reciprocal)

            if debug:
                print(f"📊 Match (rank {best_rank}, similarity {best_similarity:.2f}):")
                print(f"Ground truth: {truth_text[:80]}...")
                print(f"Retrieved: {text[:80]}...")
                print(f"Weighted score: {weighted_reciprocal:.3f}\n")
        else:
            reciprocal_ranks.append(0)
            if debug:
                print(f"⚠️ No match found for: {truth_text[:80]}...\n")

    # Calculate final MRR (average of best matches)
    mrr_score = sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0

    if debug:
        print("\n=== FINAL MRR AND RECALL CALCULATION ===")
        print(f"Individual scores: {reciprocal_ranks}")
        print(f"MRR Score: {mrr_score:.3f}")

    return mrr_score


def clean_and_stem(text):
    """Remove noise, lowercase, stem, and keep only key terms."""
    # Step 1: Remove special chars/extra spaces
    text = re.sub(r'[^\w\s]', '', str(text))
    text = re.sub(r'\s+', ' ', text).strip().lower()

    # Step 2: Tokenize and stem (e.g., "regulating" → "regulat")
    words = word_tokenize(text)
    stemmed_words = [stemmer.stem(word) for word in words]

    # Step 3: Filter for key terms (customize your keywords)
    key_terms = {'zener', 'volt', 'regul', 'current', 'bias', 'diode'}
    filtered_words = [word for word in stemmed_words if word in key_terms]

    return ' '.join(filtered_words)

# --- TF-IDF Similarity ---
def tfidf_similarity(query, documents):
    """Calculate cosine similarity between query and docs using TF-IDF."""
    vectorizer = TfidfVectorizer()
    tfidf_matrix = vectorizer.fit_transform([query] + documents)
    similarities = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:])
    return similarities.flatten()

def calculate_recall_at_k(retrieved_sections, ground_truth_texts, metadata_sorted, k=3, similarity_threshold=0.8):
    # Clean inputs
    ground_truth_cleaned = [clean_text(text) for text in ground_truth_texts]
    top_k_cleaned = [clean_text(meta['text']) for meta in metadata_sorted[:k]]

    # Count matches
    matched = 0
    for truth_text in ground_truth_cleaned:
        for retrieved_text in top_k_cleaned:
            similarity = SequenceMatcher(None, truth_text, retrieved_text).ratio()
            if similarity >= similarity_threshold:
                matched += 1
                break  # Count each ground truth only once

    recall = matched / len(ground_truth_cleaned) if ground_truth_cleaned else 0
    print("R@K : ", recall)
    return recall
def play_video_from_timeframe(video_id, start_time, end_time):
    """
    Function to play the video from a specific time range.
    video_id: The ID or path of the video file (could be a file path or URL)
    start_time: The start time in seconds from where the video should start
    end_time: The end time in seconds where the video should stop
    """
    video_path = f"C:/Users/Rimsha Fatima/Downloads/MP/MP/{video_id}.mp4"  # Assuming the videos are stored in a "videos" folder

    # Display the video in Streamlit (using the start and end time)
    st.video(video_path, start_time=start_time, end_time=end_time)

st.title("Query Based Video Segement Retrieval System Using Knowledge Graphs")

# Taking input from the user
query_text = st.text_area("Enter your query:", height=150)

# If the user clicks the button, process the query
if st.button("Submit"):
    if query_text:
        st.write("Processing your query...")

        # Preprocess the text
        preprocessed_query = preprocess_text(query_text)

        # Initialize Neo4j session and pass the dynamic query_text
        with driver.session() as session:
            final_scores, metadata =hybrid_combined_reranker(
                query_text=query_text,
                model=model,
                driver=driver,
                fetch_all_section_embeddings=fetch_all_section_embeddings,
                get_top_k_similar_sections=get_top_k_similar_sections,
                fetch_metadata_for_sections=fetch_metadata_for_sections,
                k=3,
                alpha=0.8
            )
            mrr = calculate_mrr_hybrid(final_scores_sorted=final_scores,ground_truth_texts=ground_truth_texts,
                                         metadata_sorted=metadata,similarity_threshold=0.3, debug=True)
            recall_at_3 = calculate_recall_at_k(final_scores, ground_truth_texts, metadata, k=5)

            st.write(f"MRR: {mrr:.3f} | Recall@3: {recall_at_3:.3f}")


    else:
        st.write("Please enter a query to extract triples.")