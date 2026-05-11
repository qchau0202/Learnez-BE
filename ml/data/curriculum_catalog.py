"""Structured academic data for a realistic Computer Science curriculum.

This file serves as the source of truth for realistic course metadata, 
providing the LLM pipeline with rich context for learning path 
recommendations and dropout risk explanations.
"""

from typing import Any, Dict, List

CURRICULUM_DATA: List[Dict[str, Any]] = [
    {
        "title": "Cơ sở lập trình",
        "code": "501042",
        "description": "Introduction to programming using C/C++. Covers variables, control structures, functions, and arrays.",
        "modules": [
            {
                "title": "Kiểu dữ liệu và Cấu trúc điều khiển",
                "description": "Làm quen với cú pháp C++, biến, hằng và các cấu trúc rẽ nhánh, lặp.",
                "assignments": [
                    {"title": "Logic & Arithmetic Lab", "description": "Implement a geometric calculator with input validation.", "score": 100},
                    {"title": "Control Flow Quiz", "description": "Multiple choice assessment on nested loops.", "score": 20}
                ]
            },
            {
                "title": "Functions and Recursion",
                "description": "Modularizing code and understanding the call stack.",
                "assignments": [
                    {"title": "Recursive Fibonacci Lab", "description": "Compare iterative vs recursive performance.", "score": 100}
                ]
            }
        ]
    },
    {
        "title": "Cấu trúc dữ liệu và giải thuật",
        "code": "502043",
        "description": "Fundamental data structures including linked lists, stacks, queues, trees, and graphs, alongside sorting and searching algorithms.",
        "modules": [
            {
                "title": "Danh sách liên kết và Ngăn xếp",
                "description": "Cấu trúc dữ liệu tuyến tính và ứng dụng trong quản lý bộ nhớ.",
                "assignments": [
                    {"title": "Undo System Project", "description": "Build an undo/redo manager using stacks.", "score": 100}
                ]
            },
            {
                "title": "Cây nhị phân và Đồ thị",
                "description": "Duyệt cây và các thuật toán tìm đường trên đồ thị.",
                "assignments": [
                    {"title": "Graph Traversal Lab", "description": "Implement BFS and DFS on a directional graph.", "score": 100}
                ]
            }
        ]
    },
    {
        "title": "Database Management Systems",
        "code": "CS301",
        "description": "Relational database design using SQL. Covers normalization, transaction integrity, and indexing for performance.",
        "modules": [
            {
                "title": "SQL and Normalization",
                "description": "Mastering DDL/DML and ensuring data integrity through 3NF/BCNF.",
                "assignments": [
                    {"title": "Schema Design Project", "description": "Normalize a legacy spreadsheet into a relational schema.", "score": 100}
                ]
            }
        ]
    },
    {
        "title": "Software Engineering",
        "code": "CS401",
        "description": "Methodologies for large-scale software development. Focuses on SDLC, Agile (Scrum), Design Patterns, and automated testing.",
        "modules": [
            {
                "title": "Design Patterns",
                "description": "Implementing Creational, Structural, and Behavioral patterns.",
                "assignments": [
                    {"title": "Pattern Implementation Lab", "description": "Apply Singleton and Observer patterns to a UI event system.", "score": 100}
                ]
            }
        ]
    }
]