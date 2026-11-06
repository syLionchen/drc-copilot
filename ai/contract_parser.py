# LLM-based DRC contract parser: reads the sample contract PDF
# (data/sample_contract.pdf via pdfplumber) and extracts structured
# parameters (split tiers, cap amount, frequency) as validated JSON,
# using the provider selected by LLM_PROVIDER (anthropic / deepseek).
