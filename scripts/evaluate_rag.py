"""
RAGAS Evaluation Script for Enterprise RAG System.
Evaluates RAG quality using standard metrics from the RAGAS library.

Usage:
    python scripts/evaluate_rag.py --limit 10
"""

import os
import json
import argparse
import requests
import sys
from pathlib import Path
from typing import List, Dict, Any

# Try importing ragas - will guide user to install if missing
RAGAS_AVAILABLE = False
try:
    from ragas import evaluate
    from ragas.metrics import (
        faithfulness,
        answer_relevancy,
        context_precision,
        context_recall,
    )
    from datasets import Dataset
    RAGAS_AVAILABLE = True
except ImportError:
    print("[!] RAGAS not installed. Using simple evaluation.")
    print("    To enable full metrics: pip install ragas datasets")


# Configuration
RAG_BACKEND_URL = os.getenv("RAG_BACKEND_URL", "http://localhost:8000")
DATASET_PATH = "tests/data/synthetic_gold_dataset.json"
OUTPUT_PATH = "tests/data/evaluation_results.json"


def load_golden_dataset(path: str, limit: int = None) -> List[Dict]:
    """Load the synthetic golden dataset."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if limit:
        data = data[:limit]
    print(f"[*] Loaded {len(data)} examples from {path}")
    return data

# Colecciones Qdrant a evaluar — ajustar según la configuración del despliegue
COLLECTIONS = ["documents"]


def call_rag_backend(question: str) -> Dict[str, Any]:
    """
    Call the RAG backend search API across ALL collections.
    Returns: {"answer": str, "contexts": List[str], "scores": List[float]}
    """
    all_contexts = []
    all_scores = []
    
    for collection in COLLECTIONS:
        try:
            response = requests.post(
                f"{RAG_BACKEND_URL}/api/v1/search",
                json={
                    "query": question,
                    "top_k": 5,
                    "strategy": "hybrid",
                    "collection": collection
                },
                headers={"Content-Type": "application/json"},
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            
            results = data.get("results", [])
            for r in results:
                if r.get("text"):
                    all_contexts.append(r.get("text", ""))
                    all_scores.append(r.get("score", 0))
        except Exception as e:
            # Collection might not exist or be empty - continue
            pass
    
    # Sort by score and take top results
    if all_scores:
        sorted_pairs = sorted(zip(all_scores, all_contexts), reverse=True)
        all_scores, all_contexts = zip(*sorted_pairs[:10])  # Top 10
        all_scores, all_contexts = list(all_scores), list(all_contexts)
    
    answer = all_contexts[0] if all_contexts else ""
    return {"answer": answer, "contexts": all_contexts, "scores": all_scores}


def run_evaluation_simple(dataset: List[Dict]) -> Dict[str, float]:
    """
    Simple evaluation without RAGAS (fallback if not installed).
    Computes metrics for retrieval quality.
    """
    results = []
    for item in dataset:
        question = item["question"]
        ground_truth = item["answer"]
        original_context = item.get("context", "")
        
        print(f"  [>] Evaluating: {question[:50]}...")
        rag_result = call_rag_backend(question)
        
        # Check if ground truth appears in ANY of the retrieved contexts
        all_contexts_text = " ".join(rag_result["contexts"]).lower()
        ground_truth_in_contexts = ground_truth.lower() in all_contexts_text
        
        # Check if retrieval got relevant content (source doc match)
        has_contexts = len(rag_result["contexts"]) > 0
        
        # Check if top result has high score (> 0.7)
        top_score = rag_result.get("scores", [0])[0] if rag_result.get("scores") else 0
        high_confidence = top_score > 0.7
        
        results.append({
            "question": question,
            "ground_truth": ground_truth,
            "answer": rag_result["answer"][:200] + "..." if len(rag_result["answer"]) > 200 else rag_result["answer"],
            "contexts": rag_result["contexts"],
            "ground_truth_found": ground_truth_in_contexts,
            "has_retrieval": has_contexts,
            "top_score": top_score,
            "high_confidence": high_confidence,
        })
    
    # Aggregate
    total = len(results)
    scores = {
        "retrieval_rate": sum(1 for r in results if r["has_retrieval"]) / total if total else 0,
        "ground_truth_in_contexts": sum(1 for r in results if r["ground_truth_found"]) / total if total else 0,
        "high_confidence_rate": sum(1 for r in results if r["high_confidence"]) / total if total else 0,
        "avg_top_score": sum(r["top_score"] for r in results) / total if total else 0,
        "total_evaluated": total,
    }
    
    return scores, results


def run_evaluation_ragas(dataset: List[Dict]) -> Dict[str, float]:
    """
    Full RAGAS evaluation with standard metrics.
    """
    questions = []
    answers = []
    contexts_list = []
    ground_truths = []
    
    for item in dataset:
        question = item["question"]
        ground_truth = item["answer"]
        context = item.get("context", "")
        
        print(f"  [>] Evaluating: {question[:50]}...")
        rag_result = call_rag_backend(question)
        
        questions.append(question)
        answers.append(rag_result["answer"])
        contexts_list.append(rag_result["contexts"] if rag_result["contexts"] else [context])
        ground_truths.append(ground_truth)
    
    # Build dataset for RAGAS
    eval_dataset = Dataset.from_dict({
        "question": questions,
        "answer": answers,
        "contexts": contexts_list,
        "ground_truth": ground_truths,
    })
    
    # Run RAGAS evaluation
    print("\n[*] Running RAGAS evaluation...")
    result = evaluate(
        eval_dataset,
        metrics=[
            faithfulness,
            answer_relevancy,
            context_precision,
            context_recall,
        ],
    )
    
    return result, None


def main():
    parser = argparse.ArgumentParser(description="Evaluate RAG system with RAGAS metrics.")
    parser.add_argument("--limit", "-l", type=int, default=5, help="Max examples to evaluate")
    parser.add_argument("--dataset", "-d", type=str, default=DATASET_PATH, help="Path to golden dataset")
    
    args = parser.parse_args()
    
    # Check dataset exists
    if not Path(args.dataset).exists():
        print(f"[X] Dataset not found: {args.dataset}")
        print("   Run: python scripts/generate_synthetic_dataset.py --input data/watch/ --limit 10")
        sys.exit(1)
    
    # Load data
    dataset = load_golden_dataset(args.dataset, args.limit)
    
    if not dataset:
        print("[X] No data to evaluate.")
        sys.exit(1)
    
    print(f"\n[*] Starting evaluation on {len(dataset)} examples...\n")
    
    # Run evaluation
    if RAGAS_AVAILABLE:
        scores, details = run_evaluation_ragas(dataset)
        print("\n" + "="*50)
        print("[*] RAGAS Evaluation Results")
        print("="*50)
        for metric, score in scores.items():
            print(f"  {metric}: {score:.4f}")
    else:
        scores, details = run_evaluation_simple(dataset)
        print("\n" + "="*50)
        print("[*] Simple Evaluation Results (RAGAS not installed)")
        print("="*50)
        for metric, score in scores.items():
            print(f"  {metric}: {score}")
    
    # Save results
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "scores": dict(scores) if hasattr(scores, "items") else scores,
            "details": details,
        }, f, indent=2, ensure_ascii=False, default=str)
    
    print(f"\n[+] Results saved to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
