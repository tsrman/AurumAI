import type { KnowledgeBase, Model } from "@/types/aurum";

// Demo fixtures for the unauthenticated mock mode and tests; kept in English.

export const models: Model[] = [
  {
    id: "pro",
    name: "AurumAI 2 · Reasoning",
    shortName: "Reasoning",
    description: "Deep analysis with a chain of reasoning",
  },
  {
    id: "fast",
    name: "AurumAI 2 · Fast",
    shortName: "Fast",
    description: "Instant answers without long thinking",
  },
  {
    id: "local",
    name: "AurumAI · Local",
    shortName: "Local",
    description: "On-premises, no internet access",
  },
];

export const knowledgeBases: KnowledgeBase[] = [
  {
    id: "kb1",
    name: "Procedures and instructions",
    badge: "PI",
    badgeColor: "green",
    description:
      "Process procedures, work and job instructions of the processing plant.",
    docCount: 248,
    size: "1.4 GB",
    updated: "2 h ago",
    status: "indexed",
    docs: [
      {
        name: "Procedure TR-04 “Cyanidation”.pdf",
        type: "PDF",
        size: "2.1 MB",
        chunks: 184,
        status: "indexed",
        date: "14.06",
      },
      {
        name: "Procedure TR-02 “Flotation”.pdf",
        type: "PDF",
        size: "1.8 MB",
        chunks: 156,
        status: "indexed",
        date: "14.06",
      },
      {
        name: "Instruction I-07 “Process control system”.docx",
        type: "DOCX",
        size: "640 KB",
        chunks: 92,
        status: "indexed",
        date: "12.06",
      },
      {
        name: "Operator job instruction.docx",
        type: "DOCX",
        size: "210 KB",
        chunks: 38,
        status: "indexed",
        date: "10.06",
      },
    ],
  },
  {
    id: "kb2",
    name: "Plant process charts",
    badge: "PC",
    badgeColor: "blue",
    description:
      "Process charts by stage: grinding, flotation, sorption, desorption.",
    docCount: 76,
    size: "520 MB",
    updated: "yesterday",
    status: "indexed",
    docs: [
      {
        name: "PC-12 “Pulp alkalization”.pdf",
        type: "PDF",
        size: "880 KB",
        chunks: 64,
        status: "indexed",
        date: "09.06",
      },
      {
        name: "PC-08 “Grinding”.pdf",
        type: "PDF",
        size: "1.2 MB",
        chunks: 98,
        status: "indexed",
        date: "08.06",
      },
    ],
  },
  {
    id: "kb3",
    name: "Industrial safety (OHS)",
    badge: "IS",
    badgeColor: "amber",
    description:
      "Occupational health, industrial safety and emergency instructions.",
    docCount: 134,
    size: "760 MB",
    updated: "3 days ago",
    status: "indexed",
    docs: [
      {
        name: "Cyanide handling instruction.pdf",
        type: "PDF",
        size: "1.1 MB",
        chunks: 110,
        status: "indexed",
        date: "05.06",
      },
    ],
  },
  {
    id: "kb4",
    name: "Equipment datasheets",
    badge: "ED",
    badgeColor: "violet",
    description:
      "Datasheets, manuals and diagrams of processing plant equipment.",
    docCount: 311,
    size: "3.2 GB",
    updated: "indexing…",
    status: "processing",
    docs: [
      {
        name: "Mill MShR-3600 datasheet.pdf",
        type: "PDF",
        size: "4.4 MB",
        chunks: 0,
        status: "processing",
        date: "now",
      },
      {
        name: "Flotation machine RIF-25 manual.pdf",
        type: "PDF",
        size: "3.1 MB",
        chunks: 288,
        status: "indexed",
        date: "15.06",
      },
    ],
  },
];
