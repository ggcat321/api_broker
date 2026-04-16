try:
    import file_with_syntax_error
    print("Imported")
except Exception as e:
    print("Caught:", type(e))
