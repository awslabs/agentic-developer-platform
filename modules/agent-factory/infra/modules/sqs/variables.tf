variable "name_prefix" { type = string }
variable "visibility_timeout" { type = number; default = 900 }
variable "message_retention" { type = number; default = 14400 }
variable "max_receive_count" { type = number; default = 3 }
variable "tags" { type = map(string); default = {} }
